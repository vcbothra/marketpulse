#!/usr/bin/env python3
"""
Market Pulse — nightly snapshot generator.

Fetches every price from a chain of sources (primary → backup → backup),
then writes:
  docs/index.html                 latest snapshot
  docs/archive/YYYY-MM-DD.html    one page per night (kept forever)
  docs/archive/index.html         list of all nights
  docs/data/YYYY-MM-DD.json       raw numbers + which source was used

Run locally:  pip install -r requirements.txt && python generate.py
"""
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


class SourceError(Exception):
    pass


def get(url, **kw):
    """GET with one retry. Raises SourceError on failure."""
    last = None
    for attempt in range(2):
        try:
            r = S.get(url, timeout=25, **kw)
            if r.status_code == 200:
                return r
            last = SourceError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = SourceError(str(e)[:120])
        time.sleep(2)
    raise last


def num(s):
    if s is None:
        raise SourceError("missing number")
    t = re.sub(r"[^\d.\-]", "", str(s).replace("−", "-"))
    if t in ("", "-", "."):
        raise SourceError(f"bad number {s!r}")
    return float(t)


MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def mon(name):
    return MON.get(name[:3].lower())


def fmt_date(d):
    return f"{d.day} {d.strftime('%b')}" if d else ""


# ---------------------------------------------------------------- sources
# Each returns dict(price=, prev=, asof=date|None, note=str)

def yahoo(sym):
    last = None
    for host in ("query1", "query2"):
        try:
            r = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}",
                    params={"range": "5d", "interval": "1d"})
            res = r.json()["chart"]["result"][0]
            m = res["meta"]
            price = m["regularMarketPrice"]
            off = m.get("gmtoffset", 0)
            mkt_day = dt.datetime.fromtimestamp(m["regularMarketTime"] + off, dt.timezone.utc).date()
            prev = m.get("previousClose")
            if prev is None:
                # Previous close = last daily close dated *before* the market day.
                # Bars with no close (holidays) are skipped, so a market that was shut
                # today still compares its last session with the one before it.
                ts = res.get("timestamp") or []
                closes = res["indicators"]["quote"][0]["close"]
                bars = [(dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date(), c)
                        for t, c in zip(ts, closes) if c is not None]
                earlier = [c for d, c in bars if d < mkt_day]
                if earlier:
                    prev = earlier[-1]
            if prev is None:
                raise SourceError("no previous close")
            return dict(price=float(price), prev=float(prev), asof=mkt_day)
        except (SourceError, KeyError, IndexError, TypeError, ValueError) as e:
            last = e
    raise SourceError(f"yahoo {sym}: {last}")


def cnbc(sym):
    r = get("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
            params={"symbols": sym, "requestMethod": "itv", "noform": "1", "partnerId": "2",
                    "fund": "1", "exthrs": "1", "output": "json"})
    try:
        q = r.json()["FormattedQuoteResult"]["FormattedQuote"][0]
        price = num(q["last"])
        prev = num(q["previous_day_closing"]) if q.get("previous_day_closing") else price - num(q["change"])
        asof = None
        if q.get("last_time"):
            asof = dt.date.fromisoformat(q["last_time"][:10])
        return dict(price=price, prev=prev, asof=asof)
    except (KeyError, IndexError, ValueError) as e:
        raise SourceError(f"cnbc {sym}: {e}")


def google(sym):
    r = get(f"https://www.google.com/finance/quote/{sym}", params={"hl": "en"})
    t = r.text
    m = re.search(r'data-last-price="([\d.]+)"', t)
    if not m:
        raise SourceError("google: no price")
    price = float(m.group(1))
    text = BeautifulSoup(t, "html.parser").get_text("|", strip=True)
    p = re.search(r"Previous close\|[^\d|]*([\d,]+\.?\d*)", text)
    if not p:
        raise SourceError("google: no previous close")
    asof = None
    ts = re.search(r'data-last-normal-market-timestamp="(\d+)"', t)
    if ts:
        asof = dt.datetime.fromtimestamp(int(ts.group(1)), IST).date()
    return dict(price=price, prev=num(p.group(1)), asof=asof)


def tradingeconomics(slug, factor=1.0):
    r = get(f"https://tradingeconomics.com/commodity/{slug}")
    soup = BeautifulSoup(r.text, "html.parser")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    desc = meta["content"] if meta and meta.get("content") else r.text
    m = re.search(r"(?:rose|fell|increased|decreased|climbed|dropped|declined|traded|was)[^0-9]{0,40}"
                  r"([\d,]+\.?\d*)\s*USD/(\w+)[^.]*?(up|down)\s*([\d.]+)%\s*from the previous day", desc, re.I)
    if not m:
        raise SourceError("tradingeconomics: pattern not found")
    price = num(m.group(1))
    pct = float(m.group(4)) * (1 if m.group(3).lower() == "up" else -1)
    prev = price / (1 + pct / 100)
    return dict(price=price * factor, prev=prev * factor, asof=None)


def westmetall(field):
    r = get("https://www.westmetall.com/en/markdaten.php", params={"action": "table", "field": field})
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        m = re.match(r"^(\d{1,2})\.?\s+([A-Za-z]+)\.?\s+(\d{4})$", cells[0])
        if not m or not mon(m.group(2)):
            continue
        try:
            v = num(cells[1])
        except SourceError:
            continue
        if v > 0:
            rows.append((dt.date(int(m.group(3)), mon(m.group(2)), int(m.group(1))), v))
    rows.sort(reverse=True)
    if len(rows) < 2:
        raise SourceError(f"westmetall {field}: no rows")
    return dict(price=rows[0][1], prev=rows[1][1], asof=rows[0][0])


def lbma(file):
    r = get(f"https://prices.lbma.org.uk/json/{file}.json")
    try:
        rows = [(x["d"], x["v"][0]) for x in r.json() if x.get("v") and isinstance(x["v"][0], (int, float))]
    except (ValueError, TypeError, KeyError) as e:
        raise SourceError(f"lbma {file}: {e}")
    rows.sort()
    if len(rows) < 2:
        raise SourceError(f"lbma {file}: no rows")
    return dict(price=float(rows[-1][1]), prev=float(rows[-2][1]),
                asof=dt.date.fromisoformat(rows[-1][0]), series=dict(rows[-40:]))


_ibja_cache = {}


def ibjarates():
    if "v" in _ibja_cache:
        return _ibja_cache["v"]
    r = get("https://ibjarates.com/")
    soup = BeautifulSoup(r.text, "html.parser")

    def session_of(el):
        for p in [el, *el.parents]:
            if getattr(p, "name", None) in (None, "body", "[document]"):
                break
            tag = (p.get("id") or "") + " " + " ".join(p.get("class") or [])
            tag = tag.lower()
            if re.search(r"(^|[^a-z])pm([^a-z]|$)", tag):
                return "PM"
            if re.search(r"(^|[^a-z])am([^a-z]|$)", tag):
                return "AM"
        return None

    obs, seen = [], {}
    for table in soup.find_all("table"):
        gi, si = 1, 6
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            s_idx = next((i for i, c in enumerate(cells) if re.search("silver", c, re.I)), -1)
            if s_idx > 0 and any(re.search("date", c, re.I) for c in cells):
                si = s_idx
                g = next((i for i, c in enumerate(cells) if re.fullmatch(r"(gold\s*)?999", c, re.I)), -1)
                if g > 0:
                    gi = g
                continue
            m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", cells[0])
            if not m or len(cells) <= max(gi, si):
                continue
            d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            try:
                gold = num(cells[gi])
            except SourceError:
                continue
            try:
                silver = num(cells[si])
            except SourceError:
                silver = None
            if not (20000 < gold < 2_000_000):
                continue
            sess = session_of(tr)
            if not sess:
                seen[d] = seen.get(d, 0) + 1
                sess = "AM" if seen[d] == 1 else "PM"
            obs.append(dict(date=d, session=sess, gold=gold,
                            silver=silver if silver and 20000 < silver < 5_000_000 else None))

    if not obs:
        raise SourceError("ibjarates: no rows")

    # Current-day table (only used when it carries its own date)
    latest = max(o["date"] for o in obs)
    ref = max(obs, key=lambda o: (o["date"], o["session"]))
    for table in soup.find_all("table"):
        ctx = table.get_text(" ", strip=True)
        par = table.find_parent()
        if par:
            ctx = par.get_text(" ", strip=True)[:600]
        dm = re.search(r"(\d{2})/(\d{2})/(\d{4})", ctx)
        if not dm:
            continue
        d = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        if d <= latest:
            continue
        tg = ts = None
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells or re.search(r"\d{2}/\d{2}/\d{4}", cells[0]):
                continue
            vals = []
            for c in cells[1:]:
                try:
                    vals.append(num(c))
                except SourceError:
                    pass
            if not vals:
                continue
            if tg is None and re.match(r"^(gold\s*)?999\b", cells[0], re.I):
                tg = vals
            elif ts is None and re.search("silver", cells[0], re.I):
                ts = vals
        near = lambda v, r: v and r and abs(v / r - 1) < 0.15
        if tg:
            for i, sess in enumerate(("AM", "PM")):
                if i < len(tg) and near(tg[i], ref["gold"]):
                    sv = ts[i] if ts and i < len(ts) and near(ts[i], ref["silver"]) else None
                    obs.append(dict(date=d, session=sess, gold=tg[i], silver=sv))

    key = lambda o: (o["date"], 1 if o["session"] == "PM" else 0)
    uniq = {key(o): o for o in obs}
    _ibja_cache["v"] = [uniq[k] for k in sorted(uniq)]
    return _ibja_cache["v"]


def ibja_series(metal):
    seq = [o for o in ibjarates() if o[metal]]
    if not seq:
        raise SourceError(f"ibjarates: no {metal}")
    last = seq[-1]
    prev = next((o for o in reversed(seq[:-1]) if o["date"] < last["date"] and o["session"] == last["session"]), None)
    if prev is None and len(seq) > 1:
        prev = seq[-2]
    return dict(price=last[metal], prev=prev[metal] if prev else None, asof=last["date"], session=last["session"])


def ibja_co_gold():
    r = get("https://www.ibja.co/")
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    m = re.search(r"Fine\s*Gold\s*\(?999\)?\s*:?\s*₹?\s*([\d,]+)", text, re.I)
    if not m:
        raise SourceError("ibja.co: gold not found")
    per_g = num(m.group(1))
    d = re.search(r"\((AM|PM)\)\s*-?\s*(\d{2})/(\d{2})/(\d{4})", text)
    asof = dt.date(int(d.group(4)), int(d.group(3)), int(d.group(2))) if d else None
    return dict(price=per_g * 10, prev=None, asof=asof, session=d.group(1) if d else "")


RUPEE = r"(?:₹|Rs\.?|INR)\s*"
NUMBER = r"([\d,]+(?:\.\d+)?)"
SIGNED = r"([+\-−]?\s*[\d,]+(?:\.\d+)?)"


def _groww_date(day, mon_name, yr):
    y = int(yr)
    y = y + 2000 if y < 100 else y
    m = mon(mon_name)
    return dt.date(y, m, int(day)) if m else None


def groww(metal):
    """Groww daily rates (they republish the IBJA benchmark).
    gold → ₹ per 10 g (24K), silver → ₹ per kg (999)."""
    slug, label, per10_to_unit, per_g_to_unit = (
        ("gold-rates", r"24\s*K\s*Gold", 1, 10) if metal == "gold" else ("silver-rates", r"Silver", 100, 1000))
    r = get(f"https://groww.in/{slug}")
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    text = text.replace(" ", " ")
    # Headline card, e.g. "24K Gold / 10gm 25 Sep '26 ₹1,52,670.00 +1,885.00(1.25%)"
    m = re.search(label + r"\s*/\s*10\s*gm\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+'?(\d{2,4})\s*" + RUPEE + NUMBER +
                  r"\s*" + SIGNED + r"\s*\(", text, re.I)
    if m:
        price = num(m.group(4)) * per10_to_unit
        change = num(m.group(5).replace(" ", "")) * per10_to_unit
        return dict(price=price, prev=price - change, asof=_groww_date(m.group(1), m.group(2), m.group(3)))
    # Fallback: "... stands at ₹15,267.00 per gram for 24 karat ..."
    m = re.search(r"stands at\s*" + RUPEE + NUMBER + r"\s*per\s*gram", text, re.I)
    if not m:
        raise SourceError(f"groww {metal}: price not found")
    price = num(m.group(1)) * per_g_to_unit
    d = re.search(r"Today in India\s*\((\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\)", text)
    asof = _groww_date(*d.groups()) if d else None
    prev = None
    for hm in re.finditer(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s*\|?\s*" + RUPEE + NUMBER, text):
        hd = _groww_date(hm.group(1), hm.group(2), hm.group(3))
        v = num(hm.group(4))
        for k in (1, 10, 100, 1000):  # history may be per gram or per 10 g
            if abs(v * k / price - 1) < 0.06:
                v = v * k
                break
        else:
            continue
        if asof and hd and hd < asof:
            prev = v
            break
    return dict(price=price, prev=prev, asof=asof)


def mcx_5paisa(metal):
    """MCX futures (near contract) from 5paisa. gold → ₹/10 g, silver → ₹/kg."""
    r = get(f"https://www.5paisa.com/commodity-trading/mcx-{metal}-price")
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True).replace(" ", " ")
    pm = re.search(r"Previous\s*Close\s*\|?\s*:?\s*" + r"(?:₹\s*)?" + NUMBER, text, re.I)
    if not pm:
        raise SourceError(f"5paisa {metal}: previous close not found")
    prev = num(pm.group(1))
    price = None
    for m in re.finditer(r"₹\s*" + NUMBER, text):
        v = num(m.group(1))
        if abs(v / prev - 1) < 0.10:
            price = v
            break
    if price is None:
        raise SourceError(f"5paisa {metal}: price not found")
    asof = None
    d = re.search(r"As on\s*(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", text, re.I)
    if d and mon(d.group(2)):
        asof = dt.date(int(d.group(3)), mon(d.group(2)), int(d.group(1)))
    return dict(price=price, prev=prev, asof=asof)


_fx_cache = {}


def frankfurter():
    if "v" in _fx_cache:
        return _fx_cache["v"]
    start = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    r = get(f"https://api.frankfurter.dev/v1/{start}..",
            params={"from": "USD", "to": "INR,EUR,JPY,GBP,CAD,SEK,CHF"})
    rates = r.json()["rates"]
    days = sorted(rates)
    if len(days) < 2:
        raise SourceError("frankfurter: not enough days")
    _fx_cache["v"] = (days[-1], rates[days[-1]], rates[days[-2]])
    return _fx_cache["v"]


def ecb_usdinr():
    d, cur, prev = frankfurter()
    return dict(price=cur["INR"], prev=prev["INR"], asof=dt.date.fromisoformat(d))


def ecb_dxy():
    """US Dollar Index computed from ECB reference rates with the ICE formula."""
    d, cur, prev = frankfurter()

    def dxy(x):
        return (50.14348112 * x["EUR"] ** 0.576 * x["JPY"] ** 0.136 * x["GBP"] ** 0.119 *
                x["CAD"] ** 0.091 * x["SEK"] ** 0.042 * x["CHF"] ** 0.036)
    return dict(price=dxy(cur), prev=dxy(prev), asof=dt.date.fromisoformat(d))


_cg = {}


def coingecko(coin):
    if "v" not in _cg:
        r = get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"})
        _cg["v"] = r.json()
    x = _cg["v"][coin]
    p, c = float(x["usd"]), float(x["usd_24h_change"])
    return dict(price=p, prev=p / (1 + c / 100), asof=dt.datetime.now(IST).date())


def coinbase(pair):
    x = get(f"https://api.exchange.coinbase.com/products/{pair}/stats").json()
    return dict(price=num(x["last"]), prev=num(x["open"]), asof=dt.datetime.now(IST).date())


def kraken(pair):
    j = get("https://api.kraken.com/0/public/Ticker", params={"pair": pair}).json()
    if j.get("error"):
        raise SourceError(f"kraken: {j['error']}")
    x = next(iter(j["result"].values()))
    return dict(price=num(x["c"][0]), prev=num(x["o"]), asof=dt.datetime.now(IST).date())


LB_PER_T = 2204.62


def conv(fn, k):
    def f():
        x = fn()
        return {**x, "price": x["price"] * k, "prev": x["prev"] * k if x.get("prev") is not None else None}
    return f


# ---------------------------------------------------------------- catalogue
# Each item: list of (source label, fetch fn, overrides). First valid result wins.
Y = lambda s: f"https://finance.yahoo.com/quote/{requests.utils.quote(s)}"
G = lambda s: f"https://www.google.com/finance/quote/{s}"
C = lambda s: f"https://www.cnbc.com/quotes/{requests.utils.quote(s)}"
TE = lambda s: f"https://tradingeconomics.com/commodity/{s}"
WM = lambda f: f"https://www.westmetall.com/en/markdaten.php?action=table&field={f}"
LB = "https://www.lbma.org.uk/prices-and-data/precious-metal-prices"


def market(name_unit, ysym, gsym=None, csym=None):
    chain = [("Yahoo Finance", lambda: yahoo(ysym), {"href": Y(ysym)})]
    if gsym:
        chain.append(("Google Finance", lambda: google(gsym), {"href": G(gsym)}))
    if csym:
        chain.append(("CNBC", lambda: cnbc(csym), {"href": C(csym)}))
    return chain


CHAINS = {
    # India prices: IBJA benchmark → Groww (republishes IBJA) → MCX futures → (gold) IBJA's second site
    "ibjaGold": [
        ("IBJA · ibjarates.com", lambda: ibja_series("gold"), {"href": "https://ibjarates.com/", "tag": "IBJA"}),
        ("Groww", lambda: groww("gold"), {"href": "https://groww.in/gold-rates", "tag": "Groww"}),
        ("MCX futures · 5paisa", lambda: mcx_5paisa("gold"),
         {"href": "https://www.5paisa.com/commodity-trading/mcx-gold-price", "tag": "MCX futures"}),
        ("IBJA · ibja.co", ibja_co_gold, {"href": "https://www.ibja.co/", "tag": "IBJA"}),
    ],
    "ibjaSilver": [
        ("IBJA · ibjarates.com", lambda: ibja_series("silver"), {"href": "https://ibjarates.com/", "tag": "IBJA"}),
        ("Groww", lambda: groww("silver"), {"href": "https://groww.in/silver-rates", "tag": "Groww"}),
        ("MCX futures · 5paisa", lambda: mcx_5paisa("silver"),
         {"href": "https://www.5paisa.com/commodity-trading/mcx-silver-price", "tag": "MCX futures"}),
    ],
    "lbmaGold": [
        ("LBMA", lambda: lbma("gold_pm"), {"href": LB}),
        ("LBMA fix via Westmetall", lambda: westmetall("USD_ozt_London"), {"href": WM("USD_ozt_London")}),
        ("COMEX futures · Yahoo", lambda: yahoo("GC=F"), {"href": Y("GC=F"), "name": "Gold · COMEX", "tag": "futures"}),
        ("COMEX futures · CNBC", lambda: cnbc("@GC.1"), {"href": C("@GC.1"), "name": "Gold · COMEX", "tag": "futures"}),
    ],
    "lbmaSilver": [
        ("LBMA", lambda: lbma("silver"), {"href": LB}),
        ("COMEX futures · Yahoo", lambda: yahoo("SI=F"), {"href": Y("SI=F"), "name": "Silver · COMEX", "tag": "futures"}),
        ("COMEX futures · CNBC", lambda: cnbc("@SI.1"), {"href": C("@SI.1"), "name": "Silver · COMEX", "tag": "futures"}),
        ("Trading Economics", lambda: tradingeconomics("silver"), {"href": TE("silver"), "name": "Silver · spot", "tag": "spot"}),
    ],
    "brent": [
        ("Yahoo Finance", lambda: yahoo("BZ=F"), {"href": Y("BZ=F")}),
        ("CNBC", lambda: cnbc("@LCO.1"), {"href": C("@LCO.1")}),
        ("Trading Economics", lambda: tradingeconomics("brent-crude-oil"), {"href": TE("brent-crude-oil")}),
    ],
    "usdinr": [
        ("Yahoo Finance", lambda: yahoo("INR=X"), {"href": Y("INR=X")}),
        ("Google Finance", lambda: google("USD-INR"), {"href": G("USD-INR")}),
        ("ECB reference rate", ecb_usdinr, {"href": "https://www.frankfurter.app/", "tag": "ECB ref."}),
    ],
    "dxy": [
        ("Yahoo Finance", lambda: yahoo("DX-Y.NYB"), {"href": Y("DX-Y.NYB")}),
        ("CNBC", lambda: cnbc(".DXY"), {"href": C(".DXY")}),
        ("Computed from ECB rates", ecb_dxy, {"href": "https://www.frankfurter.app/", "tag": "computed"}),
    ],
    "copper": [
        ("LME via Westmetall", lambda: westmetall("LME_Cu_cash"), {"href": WM("LME_Cu_cash")}),
        ("COMEX · Trading Economics", conv(lambda: tradingeconomics("copper"), LB_PER_T),
         {"href": TE("copper"), "name": "Copper · COMEX", "tag": "from US$/lb"}),
        ("COMEX futures · Yahoo", conv(lambda: yahoo("HG=F"), LB_PER_T),
         {"href": Y("HG=F"), "name": "Copper · COMEX", "tag": "from US$/lb"}),
    ],
    "zinc": [
        ("LME via Westmetall", lambda: westmetall("LME_Zn_cash"), {"href": WM("LME_Zn_cash")}),
        ("Trading Economics", lambda: tradingeconomics("zinc"), {"href": TE("zinc")}),
    ],
    "btc": [
        ("CoinGecko", lambda: coingecko("bitcoin"), {"href": "https://www.coingecko.com/en/coins/bitcoin"}),
        ("Coinbase", lambda: coinbase("BTC-USD"), {"href": "https://www.coinbase.com/price/bitcoin"}),
        ("Kraken", lambda: kraken("XBTUSD"), {"href": "https://www.kraken.com/prices/bitcoin"}),
    ],
    "eth": [
        ("CoinGecko", lambda: coingecko("ethereum"), {"href": "https://www.coingecko.com/en/coins/ethereum"}),
        ("Coinbase", lambda: coinbase("ETH-USD"), {"href": "https://www.coinbase.com/price/ethereum"}),
        ("Kraken", lambda: kraken("ETHUSD"), {"href": "https://www.kraken.com/prices/ethereum"}),
    ],
    "nifty": market("", "^NSEI", "NIFTY_50:INDEXNSE", ".NSEI"),
    "sensex": market("", "^BSESN", "SENSEX:INDEXBOM", ".BSESN"),
    "spx": market("", "^GSPC", ".INX:INDEXSP", ".SPX"),
    "ixic": market("", "^IXIC", ".IXIC:INDEXNASDAQ", ".IXIC"),
    "n225": market("", "^N225", "NI225:INDEXNIKKEI", ".N225"),
    "hsi": market("", "^HSI", "HSI:INDEXHANGSENG", ".HSI"),
    "kospi": market("", "^KS11", "KOSPI:KRX", ".KS11"),
    "csi300": market("", "000300.SS", "000300:SHA", ".CSI300"),
}

# plausible ranges — anything outside is treated as a bad read and the next source is tried
BOUNDS = {
    "ibjaGold": (30_000, 1_500_000), "ibjaSilver": (30_000, 3_000_000),
    "lbmaGold": (500, 20_000), "lbmaSilver": (5, 500), "brent": (10, 300),
    "usdinr": (50, 200), "dxy": (50, 200), "copper": (2_000, 50_000), "zinc": (800, 20_000),
    "btc": (1_000, 10_000_000), "eth": (10, 1_000_000),
}

# ---------------------------------------------------------------- layout
SECTIONS = [
    {"title": "Bullion & Crude", "src": "India · LBMA · Brent", "groups": [{"tiles": [
        {"id": "ibjaGold", "name": "India Gold 24K", "unit": "₹ / 10 g", "cls": "hero gold", "cur": "₹", "dp": 0, "loc": "en-IN"},
        {"id": "ibjaSilver", "name": "India Silver 999", "unit": "₹ / kg", "cls": "hero silver", "cur": "₹", "dp": 0, "loc": "en-IN"},
        {"id": "lbmaGold", "name": "LBMA Gold", "unit": "US$ / oz · PM", "cls": "gold", "cur": "$", "dp": 2},
        {"id": "lbmaSilver", "name": "LBMA Silver", "unit": "US$ / oz", "cls": "silver", "cur": "$", "dp": 2},
        {"id": "gsr", "name": "Gold / Silver", "unit": "ratio", "cur": "", "dp": 2},
        {"id": "brent", "name": "Crude Oil · Brent", "unit": "US$ / bbl", "cur": "$", "dp": 2},
    ]}]},
    {"title": "Currency", "src": "FX", "groups": [{"tiles": [
        {"id": "usdinr", "name": "USD / INR", "unit": "₹ per US$", "cur": "₹", "dp": 2},
        {"id": "dxy", "name": "US Dollar Index", "unit": "DXY", "cur": "", "dp": 2},
    ]}]},
    {"title": "Base Metals", "src": "LME · cash settlement", "groups": [{"tiles": [
        {"id": "copper", "name": "Copper", "unit": "US$ / t", "cur": "$", "dp": 0},
        {"id": "zinc", "name": "Zinc", "unit": "US$ / t", "cur": "$", "dp": 0},
    ]}]},
    {"title": "Crypto", "src": "24h change", "groups": [{"tiles": [
        {"id": "btc", "name": "Bitcoin", "unit": "BTC · US$", "cur": "$", "dp": 0},
        {"id": "eth", "name": "Ethereum", "unit": "ETH · US$", "cur": "$", "dp": 0},
    ]}]},
    {"title": "Indian Equities", "src": "NSE · BSE", "groups": [{"tiles": [
        {"id": "nifty", "name": "NIFTY 50", "unit": "NSE", "cur": "", "dp": 2, "loc": "en-IN"},
        {"id": "sensex", "name": "SENSEX", "unit": "BSE", "cur": "", "dp": 2, "loc": "en-IN"},
    ]}]},
    {"title": "Global Equities", "src": "Index levels", "groups": [
        {"label": "United States", "tiles": [
            {"id": "spx", "name": "S&P 500", "unit": "SPX", "cur": "", "dp": 2},
            {"id": "ixic", "name": "Nasdaq Composite", "unit": "IXIC", "cur": "", "dp": 2},
        ]},
        {"label": "Asia", "cls": "g4", "tiles": [
            {"id": "n225", "name": "Nikkei 225", "unit": "Japan", "cur": "", "dp": 2},
            {"id": "hsi", "name": "Hang Seng", "unit": "Hong Kong", "cur": "", "dp": 2},
            {"id": "kospi", "name": "KOSPI", "unit": "South Korea", "cur": "", "dp": 2},
            {"id": "csi300", "name": "CSI 300", "unit": "China", "cur": "", "dp": 2},
        ]},
    ]},
]


# ---------------------------------------------------------------- collect
def valid(item_id, x):
    p, q = x.get("price"), x.get("prev")
    if not isinstance(p, (int, float)) or p != p or p <= 0:
        return False
    lo, hi = BOUNDS.get(item_id, (1, 1e9))
    if not (lo <= p <= hi):
        return False
    if q is not None and (q <= 0 or abs(p / q - 1) > 0.3):
        x["prev"] = None  # keep the price, drop an implausible change
    return True


# Items whose first source can jump (e.g. a futures contract rolling over):
# every source is fetched and the value most sources agree on is used.
CROSS_CHECK = {"brent"}
AGREE = 0.02  # within 2 % counts as agreeing


def cross_check(item_id, chain, chosen, log):
    found = [chosen]
    for rank, (label, fn, extra) in enumerate(chain):
        if rank <= chosen["rank"]:
            continue
        try:
            x = fn()
            if valid(item_id, x):
                found.append({**x, "source": label, "rank": rank, **extra})
        except Exception:  # noqa: BLE001
            pass
    if len(found) < 2:
        return chosen
    prices = sorted(f["price"] for f in found)
    mid = prices[len(prices) // 2] if len(prices) % 2 else (prices[len(prices) // 2 - 1] + prices[len(prices) // 2]) / 2
    close_to_mid = [f for f in found if abs(f["price"] / mid - 1) <= AGREE]
    others = ", ".join(f"{f['source']} {f['price']:.2f}" for f in found if f is not chosen)
    if len(found) >= 3 and close_to_mid and chosen not in close_to_mid:
        pick = close_to_mid[0]  # first in chain order among the agreeing ones
        log.append(f"  xchk {item_id:<11} {chosen['source']} {chosen['price']:.2f} disagreed; using {pick['source']} ({others})")
        return {**pick, "note": f"{chosen['source']} disagreed", "rank": max(pick["rank"], 1)}
    spread = abs(prices[-1] / prices[0] - 1)
    if spread > AGREE * 1.5 and not (len(found) >= 3 and chosen in close_to_mid):
        log.append(f"  xchk {item_id:<11} sources differ by {spread:.1%} ({others}); kept {chosen['source']}")
        return {**chosen, "note": f"sources differ by {spread:.0%}", "rank": max(chosen["rank"], 1)}
    log.append(f"  xchk {item_id:<11} agrees with {others}")
    return chosen


def collect():
    results, log = {}, []
    for item_id, chain in CHAINS.items():
        results[item_id] = None
        for rank, (label, fn, extra) in enumerate(chain):
            try:
                x = fn()
                if not valid(item_id, x):
                    raise SourceError(f"implausible value {x.get('price')}")
                results[item_id] = {**x, "source": label, "rank": rank, **extra}
                log.append(f"  ok   {item_id:<11} {label:<28} {x['price']:.4f}")
                break
            except Exception as e:  # noqa: BLE001 — any failure moves on to the next source
                log.append(f"  fail {item_id:<11} {label:<28} {str(e)[:90]}")
        if item_id in CROSS_CHECK and results[item_id]:
            results[item_id] = cross_check(item_id, chain, results[item_id], log)
    # Gold/Silver ratio from whatever gold & silver we got (same family preferred)
    g, s = results.get("lbmaGold"), results.get("lbmaSilver")
    if g and s:
        if g.get("series") and s.get("series"):
            common = sorted(set(g["series"]) & set(s["series"]))
            if common:
                a = common[-1]
                b = common[-2] if len(common) > 1 else None
                results["gsr"] = dict(price=g["series"][a] / s["series"][a],
                                      prev=(g["series"][b] / s["series"][b]) if b else None,
                                      asof=dt.date.fromisoformat(a), source="Derived · LBMA fixes", rank=0, href=LB)
        if not results.get("gsr"):
            prev = g["prev"] / s["prev"] if g.get("prev") and s.get("prev") else None
            same = g["rank"] == 0 and s["rank"] == 0
            results["gsr"] = dict(price=g["price"] / s["price"], prev=prev, asof=g.get("asof"),
                                  source="Derived · " + (g['source'] if g['source'] == s['source'] else f"{g['source']} / {s['source']}"),
                                  rank=0 if same else 1,
                                  href=g.get("href"))
    else:
        results["gsr"] = None
    return results, log


def unit_for(tile, r):
    base = tile["unit"]
    if r is None:
        return base
    if tile["id"] in ("lbmaGold", "lbmaSilver") and r.get("tag"):
        base = "US$ / oz"
    parts = [base]
    if r.get("tag"):
        parts.append(r["tag"])
    if tile["id"] in ("btc", "eth"):
        parts = [base]
    elif r.get("asof"):
        d = fmt_date(r["asof"])
        if r.get("session"):
            d += " " + r["session"]
        parts.append(d)
    return " · ".join(parts)


def build_payload(results, now):
    sections = []
    for sec in SECTIONS:
        groups = []
        for g in sec["groups"]:
            tiles = []
            for t in g["tiles"]:
                r = results.get(t["id"])
                tile = {k: t[k] for k in ("id", "name", "unit", "cur", "dp") if k in t}
                tile["cls"] = t.get("cls", "")
                tile["loc"] = t.get("loc", "en-US")
                tile["unit"] = unit_for(t, r)
                if r:
                    tile.update(price=r["price"], prev=r.get("prev"), source=r["source"], rank=r["rank"], note=r.get("note"),
                                href=r.get("href", ""), name=r.get("name", t["name"]),
                                asof=r["asof"].isoformat() if r.get("asof") else None)
                tiles.append(tile)
            groups.append({"label": g.get("label", ""), "cls": g.get("cls", ""), "tiles": tiles})
        sections.append({"title": sec["title"], "src": sec["src"], "groups": groups})
    return {"generated": now.isoformat(timespec="minutes"), "sections": sections}


# ---------------------------------------------------------------- write
def render(template, payload, nav):
    data = json.dumps({**payload, "nav": nav}, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("/*__DATA__*/null", data)


def main():
    now = dt.datetime.now(IST)
    day = now.date().isoformat()
    results, log = collect()
    print(f"Market Pulse snapshot {now:%Y-%m-%d %H:%M} IST")
    print("\n".join(log))
    payload = build_payload(results, now)

    (DOCS / "archive").mkdir(parents=True, exist_ok=True)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / f"{day}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    template = (ROOT / "template.html").read_text(encoding="utf-8")
    days = sorted(p.stem for p in (DOCS / "data").glob("????-??-??.json"))

    # re-render every archive page so prev/next links stay correct
    for i, d in enumerate(days):
        pl = json.loads((DOCS / "data" / f"{d}.json").read_text(encoding="utf-8"))
        nav = {"root": "../", "prev": f"{days[i-1]}.html" if i > 0 else None,
               "next": f"{days[i+1]}.html" if i < len(days) - 1 else None,
               "latest": "../index.html", "archive": "index.html", "live": "../live.html", "day": d}
        (DOCS / "archive" / f"{d}.html").write_text(render(template, pl, nav), encoding="utf-8")

    latest_nav = {"root": "", "prev": f"archive/{days[-2]}.html" if len(days) > 1 else None, "next": None,
                  "latest": None, "archive": "archive/index.html", "live": "live.html", "day": days[-1]}
    latest_pl = json.loads((DOCS / "data" / f"{days[-1]}.json").read_text(encoding="utf-8"))
    (DOCS / "index.html").write_text(render(template, latest_pl, latest_nav), encoding="utf-8")

    # archive index
    items = []
    for d in reversed(days):
        dd = dt.date.fromisoformat(d)
        items.append(f'<li><a href="{d}.html">{dd:%a, %d %b %Y}</a></li>')
    (DOCS / "archive" / "index.html").write_text(ARCHIVE_PAGE.replace("<!--ITEMS-->", "\n".join(items)), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    missing = [k for k, v in results.items() if v is None]
    print(f"\n{len(results) - len(missing)} of {len(results)} loaded" + (f"; missing: {', '.join(missing)}" if missing else ""))
    # Fail the run (so GitHub emails you) only when most sources are down
    if len(missing) > len(results) / 2:
        sys.exit(1)


ARCHIVE_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Pulse · Archive</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#262e38;--text:#e6edf3;--muted:#8b96a3}
@media (prefers-color-scheme: light){:root{--bg:#f4f5f7;--surface:#fff;--border:#e3e6ea;--text:#14181d;--muted:#5d6773}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;padding:20px 16px 40px}
main{max-width:640px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}
p{color:var(--muted);font-size:13px;margin-bottom:16px}
p a{color:inherit}
ul{list-style:none;display:grid;gap:8px}
li a{display:block;padding:12px 14px;background:var(--surface);border:1px solid var(--border);border-radius:12px;color:inherit;text-decoration:none;font-variant-numeric:tabular-nums}
</style></head><body><main>
<h1>Market Pulse · Archive</h1>
<p>One snapshot per night, taken around 3 AM IST. <a href="../index.html">Latest</a> · <a href="../live.html">Live</a></p>
<ul>
<!--ITEMS-->
</ul></main></body></html>"""

if __name__ == "__main__":
    main()
