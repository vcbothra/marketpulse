# Market Pulse

A nightly snapshot of bullion, currency, base metals, crypto and equity prices.
The snapshot is taken around 3 AM IST, and every night's page is kept.

- `docs/index.html`: the latest snapshot
- `docs/archive/`: one page per night, plus a list of all nights
- `docs/data/`: the raw numbers for each night (JSON), including which source was used
- `docs/live.html`: the live page, which fetches fresh prices in your browser when opened

## One-time setup (about 10 minutes)

1. **Create a GitHub account** at github.com, if you don't have one.
2. **Create a new repository.** Click **+** (top right), then **New repository**.
   - Name it `market-pulse`.
   - Choose **Public**. Free GitHub Pages hosting needs a public repository. The page only shows public market prices.
   - Click **Create repository**.
3. **Upload the files.** On the new repository's page, click **uploading an existing file**. Drag in *everything inside* the unzipped `market-pulse` folder, including the `.github` folder, then click **Commit changes**.
   - On a Mac, press **Cmd + Shift + .** in Finder to show the hidden `.github` folder.
   - If dragging the folder doesn't work, create the file by hand. Click **Add file**, then **Create new file**, and name it `.github/workflows/nightly.yml`. Paste in the contents of that file.
4. **Let the job save its results.** Go to **Settings**, then **Actions**, then **General**. Under *Workflow permissions*, choose **Read and write permissions**, then click **Save**.
5. **Turn on the website.** Go to **Settings**, then **Pages**. Under *Build and deployment*, set Source to **Deploy from a branch**, Branch to **main**, and the folder to **/docs**. Click **Save**.
6. **Run it once now.** Go to the **Actions** tab, click **Nightly snapshot**, then **Run workflow**. It takes about a minute.
7. **Open your page** at `https://<your-username>.github.io/market-pulse/`. Bookmark it or add it to your phone's home screen.

From then on it runs every night by itself.

## How the backups work

Each price is tried from its sources in order. The first answer that passes a sanity check is used.

| Item | 1st | 2nd | 3rd |
|---|---|---|---|
| India Gold 24K (₹/10 g) | IBJA (ibjarates.com) | Groww | MCX futures (5paisa), then IBJA's ibja.co |
| India Silver 999 (₹/kg) | IBJA (ibjarates.com) | Groww | MCX futures (5paisa) |
| LBMA Gold | LBMA (official) | LBMA fix via Westmetall | COMEX futures (Yahoo, then CNBC) |
| LBMA Silver | LBMA (official) | COMEX futures (Yahoo, then CNBC) | Trading Economics spot |
| Brent crude | Yahoo Finance | CNBC | Trading Economics |
| USD/INR | Yahoo Finance | Google Finance | ECB reference rate |
| US Dollar Index | Yahoo Finance | CNBC | Computed from ECB rates |
| Copper | LME via Westmetall | COMEX via Trading Economics | COMEX futures via Yahoo |
| Zinc | LME via Westmetall | Trading Economics | – |
| BTC, ETH | CoinGecko | Coinbase | Kraken |
| All stock indices | Yahoo Finance | Google Finance | CNBC |

On the page, a **green dot** means the primary source was used and an **amber dot** means a backup was used.
Tap **"Where each number came from"** at the bottom of the page to see the source for every price.
If a backup isn't the same measure (for example, COMEX futures in place of the LBMA fix), the tile's name says so.

For India gold and silver, the source name appears on the tile's second line (IBJA, Groww or MCX futures), next to the date.
Groww republishes the IBJA rate, so the first two usually match. MCX is the traded futures price, so it can differ by a little.

## If something breaks

- **Failed runs.** If most sources fail on a night, the run is marked failed and GitHub emails you. The page is still saved with whatever loaded.
- **Changed websites.** If a site changes its layout, that source stops working and the next one takes over. The run log shows which sources worked. To see it, go to **Actions**, open the latest run, then **Build snapshot**.
- **Changing the time.** Edit the `cron` line in `.github/workflows/nightly.yml`. The times are in UTC, and IST = UTC + 5:30.
