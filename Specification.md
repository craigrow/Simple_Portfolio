# Simple Portfolio Tracker
### Technical Specification

### Source Control
- **Repository**: https://github.com/craigrow/Simple_Portfolio.git
- **Branching policy**: All work must be done in a feature branch, never directly on `main`. Merge to `main` via pull request only after all tests pass.

### Overview
The goal of the simple portfolio tracker is to show how a set of portfolio transactions performed against the S&P 500 and the NASDAQ. To achieve this, we will create shadow portfolios for VOO and QQQ. For each portfolio investment, we will assume the same dollar amount was invested in VOO and QQQ. 

For example, we enter, into the main portfolio, the following transaction: [Buy, MSFT, 100 shares at $100]. The total value of that transaction is $10,000. Thus, we would record an investment of $10,000 in the VOO portfolio by finding the prices of VOO on the same date and then calculating the number of shares purchased. Assuming the price of VOO was $25 when the MSFT purhase was made, we would record a purchase of 400 VOO at $25. 

The portfolio will be stored in a table with the following headings.
1. DATE: The date of the transaction.
2. TICKER: The ticker of the security. We will track individual stocks, mutual funds, ETFs and crypto.
3. PURCHASE PRICE: The price paid per share.
4. SHARES PURCHASED: The quantity of shares. Note: it is expected most transactions, in both the main portfolio and the shadow portfolios will be in fractional shares. The application needs to handle fractional shares to five decimal points.
5. TOTAL VALUE: The total value of the transaction (PURCHASE PRICE * SHARES PURCHASED).

The two shadow portfolios, for QQQ and VOO, will have the same structure.

### Non-Functional Requirements
1. We want to get security prices from a free API such as yfinance.
2. In these early sprints, we can just run the web page on the local host. However, eventually this will be an app that needs to be available 24/7 from any device. We will want to host on a site that enables us to run for free or very low cost (<$5 per month).
3. Stability and robustness are critical. A full suite of unit tests and functional tests must be created as we implement each user story. We cannot move to a new user story until the full test suite is passing. This is critical to enable us to continue enhancing the applications while remaining highly available to users.

---

## Sprint 1

### User Story 1
As a user, I want to be able to enter transactions by editing a text file and then I want the app to show a web page displaying the full table of transactions in my portfolio and the shadow portfolios.

### Status: Complete

### Tech Stack
- **Python 3.12** with **Flask** for the web framework
- **yfinance** (v1.2.0+) for fetching historical security prices
- **pandas** for data handling
- **pytest** for testing

Flask was chosen for its simplicity and compatibility with low-cost hosting options (Render, Railway, fly.io).

### Architecture Decisions

1. **Separation of concerns**: Portfolio logic lives in `portfolio_engine.py`, separate from the Flask app (`app.py`). This makes it easy to swap frameworks, add a CLI, or reuse logic elsewhere.

2. **Input file vs. processed data**: The user edits `transactions.csv` to enter new transactions. Processed data is stored separately in the `data/` directory. This separation enables easy detection of new transactions by comparing row counts between the input file and the processed portfolio.

3. **Shadow portfolio persistence**: Shadow portfolios are calculated when new transactions are detected and persisted to CSV files, rather than recalculated on every page load. This avoids unnecessary API calls to yfinance.

4. **Sync on page load**: When the web page is loaded, the engine checks for new transactions and processes them before rendering. No background process is needed for Sprint 1.

5. **Closing price for shadow portfolios**: VOO and QQQ shadow entries use the closing price on the transaction date.

6. **Date format**: `YYYY-MM-DD` is the required format in `transactions.csv`.

7. **TOTAL_VALUE is calculated**: The user provides DATE, TICKER, PURCHASE_PRICE, and SHARES_PURCHASED. TOTAL_VALUE is computed by the engine.

### Project Structure
```
Simple_Portfolio/
├── transactions.csv           # User-edited input file (tracked in git)
├── data/
│   ├── .gitkeep
│   ├── portfolio.csv          # Processed main portfolio (git-ignored)
│   ├── shadow_voo.csv         # Persisted VOO shadow (git-ignored)
│   └── shadow_qqq.csv         # Persisted QQQ shadow (git-ignored)
├── app.py                     # Flask web app — single route, syncs + renders
├── portfolio_engine.py        # Core logic: read, sync, persist, load
├── requirements.txt           # flask, yfinance, pandas
├── templates/
│   └── index.html             # Displays all 3 tables with total invested
└── tests/
    ├── test_portfolio_engine.py  # 13 unit tests
    └── test_app.py               # 6 functional tests
```

### How to Run
```bash
pip install -r requirements.txt
python app.py
# Open http://127.0.0.1:5001
```
**Note:** macOS AirPlay Receiver occupies port 5000. The app runs on port 5001 to avoid this conflict. To change the port, edit the `app.run()` call in `app.py`.

### How to Test
```bash
pip install pytest
python -m pytest tests/ -v
```

### Test Coverage (19 tests, all passing)
- CSV file creation and reading
- New transaction detection (empty, synced, appended)
- Sync: creates portfolio + shadow entries, no duplicates, incremental, empty no-op
- Fractional shares to 5 decimal places
- Shadow rows skipped when price data unavailable (e.g. non-trading day)
- Multiple transactions synced at once
- load_all returns three DataFrames
- Flask route: empty state, populated state, page title, total invested display, sync on load, all three tables present

### Known Limitations / Deferred to Future Sprints
- **Non-trading day validation**: Transactions on weekends/holidays will process to the main portfolio but won't generate shadow entries (no VOO/QQQ price available). Proper validation and error handling is deferred.
- **Sell transactions**: Only buys are supported. Sells will be needed in a future sprint.
- **No auto-refresh**: User must manually refresh the browser to see updates.
- **No duplicate detection**: If the same transaction is entered twice in `transactions.csv`, both will be processed.
- **Clearing data**: To re-sync from scratch, delete the three CSV files in `data/` and reload the page.

## Sprint 2

### User Story #2
As a user I want the app to calculate the current value of my portfolio and of the shadow portfolios and display that in the title bar above each portfolio. Note: the app should account for any splits which have occured since the purchases were made. Splits may have happened in portfolio securities and/or in the shadow securities, VOO and QQQ.

### Status: Complete

### Architecture Decisions

1. **Split data persistence**: Split history is persisted to `data/splits.csv` (columns: TICKER, DATE, RATIO). yfinance always returns full split history per ticker, so we overwrite rather than append.

2. **Staleness rule**: Splits are refreshed once per trading day. If `splits.csv` was last modified before the most recent market close (4:00 PM ET on the last trading day), re-fetch for all tickers. This avoids unnecessary API calls while ensuring we catch new splits within one trading day.

3. **Current prices fetched live**: Current prices are fetched on each page load via a batched `yf.download()` call for all unique tickers across all three portfolios. This is acceptable for localhost; caching may be needed when hosted.

4. **Per-row enrichment**: Rather than just portfolio totals, each row is enriched with CURRENT_SHARES (split-adjusted) and CURRENT_VALUE (adjusted shares × current price). The `enrich_portfolio()` function handles this and also returns the portfolio total.

5. **Split adjustment logic**: For each holding, all splits after the purchase date are applied multiplicatively to the original share count. Splits before the purchase date are ignored (the purchase price already reflects them).

### New/Modified Files
- `portfolio_engine.py` — added `_last_market_close()`, `sync_splits()`, `_fetch_splits()`, `_read_splits()`, `get_adjusted_shares()`, `enrich_portfolio()`, `_fetch_current_prices()`
- `app.py` — calls `sync_splits()` and `enrich_portfolio()` for each portfolio
- `templates/index.html` — title bar shows "Total Invested $X | Current Value $Y"; table includes CURRENT_SHARES and CURRENT_VALUE columns
- `data/splits.csv` — persisted split data (git-ignored)

### Test Coverage (33 tests, all passing)
- All Sprint 1 tests (19)
- `_last_market_close()`: returns weekday, returns 4 PM ET
- `get_adjusted_shares()`: no splits, split after purchase, split before purchase ignored, wrong ticker ignored, fractional shares
- `sync_splits()`: creates file, skips when fresh, no-op with no tickers
- `enrich_portfolio()`: current value calculation, current value with splits, per-row CURRENT_SHARES/CURRENT_VALUE, empty portfolio

### Known Limitations / Deferred to Future Sprints
- **Holiday awareness**: `_last_market_close()` handles weekends but not market holidays. A transaction or staleness check on a holiday may behave slightly off. Acceptable for now.
- **Background refresh**: Split and price refresh is triggered on page load. Eventually should be a background scheduled process.
- **Price caching**: Current prices are fetched on every page load. May need caching for hosted deployment.

---

## Sprint 3

### User Story #3
As a user, I want the app to find all the dividends that have been paid, in the main portfolio and the shadow portfolios, sum them up and display that in the title bar so that I can understand not just the price appreciation v. the indexes but the value of dividends v. the index.

### Status: Complete

### Architecture Decisions

1. **Dividend data persistence**: Dividend history is persisted to `data/dividends.csv` (columns: TICKER, DATE, AMOUNT). Uses the same staleness rule as splits — refresh once per trading day if the file was last modified before the most recent market close.

2. **Split-aware dividend calculation**: When calculating dividends for a holding, the share count is adjusted for any splits that occurred between the purchase date and each dividend date. This ensures dividend amounts reflect the actual shares held at the time of each payment.

3. **Per-row and total display**: Each row in the portfolio tables includes a TOTAL_DIVIDENDS column. The title bar shows the portfolio-wide dividend total alongside total invested and current value.

4. **Cash received model**: Dividends are tracked as cash received, not reinvested. DRIP will be a future enhancement requiring a cash account.

5. **Consistent data refresh pattern**: Dividend sync follows the same pattern as splits — `sync_dividends()` mirrors `sync_splits()` for consistency and maintainability.

### New/Modified Files
- `portfolio_engine.py` — added `sync_dividends()`, `_fetch_dividends()`, `_read_dividends()`, `get_total_dividends()`; updated `enrich_portfolio()` to include TOTAL_DIVIDENDS
- `app.py` — calls `sync_dividends()`, passes dividend totals to template
- `templates/index.html` — title bar shows "Dividends $Z"; table includes TOTAL_DIVIDENDS column
- `data/dividends.csv` — persisted dividend data (git-ignored)

### Test Coverage (43 tests, all passing)
- All Sprint 1 + 2 tests (33)
- `get_total_dividends()`: no dividends, after purchase, before purchase ignored, with split before dividend, multiple dividends, wrong ticker
- `sync_dividends()`: creates file, skips when fresh, no-op with no tickers
- `enrich_portfolio()` with dividends: per-row TOTAL_DIVIDENDS and portfolio total

### Known Limitations / Deferred to Future Sprints
- **Dividend reinvestment (DRIP)**: All dividends will eventually be assumed reinvested, requiring a cash account to track funds received.
- **Background refresh**: Dividend, split, and price refresh is triggered on page load. Eventually should be a background scheduled process.

---

## Bug Fixes (Post-Sprint 3)

### Fix: CSV append missing newline
`_append_rows()` opened files in append mode but did not verify the existing file ended with a newline. When the last line lacked a trailing newline, the first appended row was concatenated onto it, corrupting the CSV and causing a `ParserError`. Fixed by checking for and adding a trailing newline before appending.

### Fix: Date comparison in split and dividend logic
`get_adjusted_shares()` and `get_total_dividends()` compared split/dividend dates (`YYYY-MM-DD` format) against portfolio purchase dates (`M/D/YY` format) as raw strings. This produced incorrect results — purchases with dates starting with digits greater than `2` (e.g. `8/17/20`) had no splits applied, while dates starting with `1` had all historical splits applied including those before the purchase. Fixed by using `pd.to_datetime()` to parse both dates before comparison.

### Enhancement: Thousand separators in header values
Title bar dollar amounts (Total Invested, Current Value, Dividends) now display with comma thousand separators (e.g. `$11,358.40`).

### User Story #4
As a user, I want the portfolio summary data for the main portfolio and the shadow portfolios at the top of the page so I don't have to scroll down to compare portfolios.

### Status: Complete

### Architecture Decisions

1. **Summary table at top of page**: A comparison table is rendered above the detailed portfolio tables showing all three portfolios side by side. This avoids scrolling to compare performance.

2. **Difference column**: The summary table includes a "Difference" column calculated as (Main current value + dividends) − (Shadow current value + dividends). Positive means the main portfolio outperforms; negative means the shadow outperforms. The Main Portfolio row shows "—" since it is the baseline.

3. **Total invested passed from app.py**: Rather than recomputing totals in the template, `app.py` passes pre-computed `portfolio_invested`, `voo_invested`, and `qqq_invested` values.

### Modified Files
- `app.py` — passes total invested values to template
- `templates/index.html` — summary comparison table with Total Invested, Current Value, Dividends, and Difference columns
- `tests/test_app.py` — updated test to expect comma-formatted dollar amounts

### Test Coverage (43 tests, all passing)
- All previous tests (43), with `test_displays_total_invested` updated for comma formatting

### User Story #5
As a user, I want to see the total value of my portfolio (share value + dividends paid), at the end of trading, for every trading day from the first purchase so that I can understand performance over time.

### Status: Complete

### Architecture Decisions

1. **Single price download**: `fetch_all_history()` collects all unique tickers across all three portfolios and makes one `yf.download()` call. The same price data is reused for enrichment and historical value calculation — no redundant API calls.

2. **Price history caching**: Historical prices are persisted to `data/price_history.csv`. On subsequent loads, only a delta fetch is performed (from last cached date to today). New tickers trigger a full history fetch; existing tickers with incomplete early data are automatically detected and refetched.

3. **Vectorized historical calculation**: `get_historical_values()` iterates over holdings (not trading days) and uses vectorized pandas operations across all dates. This is O(holdings) instead of O(holdings × trading_days).

4. **Chart.js visualization**: A line chart (via Chart.js CDN) is rendered below the summary table showing all three portfolios over time. Blue = Main, Green = VOO, Red = QQQ.

5. **Daily value table**: A detailed table below the chart shows the exact daily values for all three portfolios.

### New/Modified Files
- `portfolio_engine.py` — added `fetch_all_history()` (with caching), `get_historical_values()`, `PRICE_HISTORY_FILE`; updated `enrich_portfolio()` to accept pre-loaded splits, dividends, and prices
- `app.py` — single price download shared across enrichment and history; passes history data to template
- `templates/index.html` — Chart.js line chart and daily value table
- `.gitignore` — added `data/price_history.csv`
- `tests/test_app.py`, `tests/test_portfolio_engine.py` — added `PRICE_HISTORY_FILE` to test fixtures to prevent cache pollution

### Test Coverage (43 tests, all passing)
- All previous tests (43), with test fixtures updated for price history cache isolation

### User Story #6
As a user, I want to be able to track multiple portfolios so I can keep track of all my portfolios which have different investments, goal, objectives and purposes. I want to be able to switch simply between portfolios, which are tracked separately but all reported on in the same way, with shadow portfolios, etc.

### Status: In Progress

### Architecture Decisions

1. **Folder-per-portfolio structure**: Each portfolio is a self-contained folder under `portfolios/`. This keeps portfolios fully isolated — no risk of cross-contamination, easy to back up or delete, and straightforward to extend with future "create/rename/delete portfolio" UI features.

2. **Portfolio identity**: Each portfolio folder contains a `config.json` with a `name` field for the display name. The folder name serves as the internal ID (used in URLs and file paths). Portfolio names are defined in code for now; a future story will allow users to create and rename portfolios from the UI.

3. **Engine parameterization**: Module-level file path constants are replaced with a `get_paths(portfolio_id)` function that returns all file paths for a given portfolio. All existing engine functions remain unchanged — they just operate on the paths returned for the selected portfolio.

4. **Web UI switching**: A dropdown at the top of the page lists all available portfolios by display name. Selecting a portfolio reloads the page with a `?portfolio=<folder_name>` query parameter. The rest of the page (summary table, chart, detail tables) renders identically regardless of which portfolio is selected.

5. **Migration**: The existing `transactions.csv` and `data/` contents are moved into `portfolios/tqqq_portfolio/`. The root-level `transactions.csv` and `data/` directory are removed. The existing portfolio is named "TQQQ Portfolio".

6. **Default portfolio**: If no `?portfolio=` parameter is provided, the app defaults to the first portfolio alphabetically.

### New Project Structure
```
Simple_Portfolio/
├── portfolios/
│   ├── tqqq_portfolio/
│   │   ├── config.json              # {"name": "TQQQ Portfolio"}
│   │   ├── transactions.csv         # User-edited input
│   │   └── data/
│   │       ├── portfolio.csv
│   │       ├── shadow_voo.csv
│   │       ├── shadow_qqq.csv
│   │       ├── splits.csv
│   │       ├── dividends.csv
│   │       └── price_history.csv
│   └── foolish_portfolio/
│       ├── config.json              # {"name": "Foolish Portfolio"}
│       ├── transactions.csv
│       └── data/
│           └── ...
├── app.py
├── portfolio_engine.py
├── requirements.txt
├── templates/
│   └── index.html
└── tests/
    ├── test_portfolio_engine.py
    └── test_app.py
```

### Migration Steps
1. Create `portfolios/tqqq_portfolio/` directory structure.
2. Move `transactions.csv` → `portfolios/tqqq_portfolio/transactions.csv`.
3. Move `data/*.csv` → `portfolios/tqqq_portfolio/data/`.
4. Create `portfolios/tqqq_portfolio/config.json` with `{"name": "TQQQ Portfolio"}`.
5. Create empty `portfolios/foolish_portfolio/` with config and empty transactions file.
6. Remove root-level `transactions.csv` and `data/` directory.
7. Update `.gitignore` to ignore `portfolios/*/data/*.csv` instead of `data/*.csv` (keep `.gitkeep`).

### Test Plan
- All existing engine and app tests continue to pass (test fixtures create temp portfolio folders).
- New tests: portfolio listing/discovery, portfolio switching via query param, default portfolio selection, config.json reading.

### Known Limitations / Deferred to Future Sprints
- **Create/rename/delete from UI**: Portfolio management is done by editing files/folders for now.
- **Portfolio-level settings**: All portfolios share the same shadow benchmarks (VOO, QQQ). Per-portfolio benchmark selection is deferred.

### User Story #7
As a user, I want to see my portfolio immediately when I put the URL in the browser, so that I don't have to wait to see my portfolios. It is understood that data may be cached. So, it is OK to show cached data on load and then refresh the data in the background provided you clearly communicate that I'm viewing cached data and refresh the page when it is available. You should also consider what data can be pre-fetched (before the user loads the page) efficiently. 

### Status: Not Started

### Analysis: Current Page Load Breakdown

**On every page load (no API calls, pure computation):**
- `enrich_portfolio()` × 3 — recalculates CURRENT_SHARES, CURRENT_VALUE, TOTAL_DIVIDENDS for every row using already-cached data (splits, dividends, prices from CSV files).
- `get_historical_values()` × 3 — rebuilds daily value series for the chart from cached price history.
- Template rendering.

**Once per trading day (staleness-gated, triggers API calls):**
- `sync_splits()` — re-fetches split history. Sequential, one `yf.Ticker()` call per unique ticker. ~30+ calls for the Foolish Portfolio. This is the primary bottleneck on the first load after market close.
- `sync_dividends()` — same pattern, one ticker at a time.
- `fetch_all_history()` — delta fetch of new price data. Already efficient (single batched `yf.download()`).

**Only when new transactions are added (infrequent, ~weekly):**
- `sync()` — processes new rows, 2 API calls per transaction (VOO + QQQ closing price). Negligible with one-at-a-time adds.

### Proposed Architecture

1. **Serve cached data immediately**: On page load, skip all staleness checks and API calls. Read existing CSV files, run enrichment (pure computation), and render. This should be near-instant.

2. **Show "cached data" indicator**: Display a banner or subtle indicator when the data is stale (splits/dividends/prices last refreshed before most recent market close). Something like "Data as of [timestamp]. Refreshing…"

3. **Background refresh via async endpoint**: After the page loads, JavaScript calls a `/refresh?portfolio=<id>` endpoint that triggers the staleness-gated sync (splits, dividends, price history delta). When complete, the frontend auto-reloads or updates the data and removes the stale indicator.

4. **Pre-fetch consideration**: A background scheduled process (cron or in-app timer) could run the daily staleness refresh shortly after market close (4:00 PM ET), so data is already fresh before the user visits. This aligns with the "auto refresh at end of trading day" backlog item and could be implemented together.

### Why not optimize the API call paths instead?
The current logic is correct, readable, and maintainable. The O(rows × dividends × splits) enrichment complexity is fine at this scale (hundreds of transactions). The real UX problem is the blocking staleness refresh on page load, which is better solved by serving cached data first than by optimizing code that works correctly.

---

### Backlog Stories
1. Zoom in and out on the performance chart.
2. Show today v. the market.
3. Put transaction data on a separate tab.
4. Show performance of each individual transaction.
5. Show "batting average"
6. Host somewhere that is accessible from any internet connected device.
7. Auto refresh at the end of the trading day.
8. User defined performance "windows"
9. Create, rename, and delete portfolios from the UI.
10. Sort the transactions in date order.
11. Make all the tables sortable by clicking on headings.
12. Make the heading "stick" when the table scrolls.
13. Create a portfolio view (group tickers from the transactions)