## Bug Log

1. `_fetch_current_prices()` requests data for a future date
2. ~~Portfolio current values appear to be wrong. Foolish Portfolio shows almost no growth and VOO shows curent value of $0.~~ Root cause is #1, `_fetch_current_prices()` requests data for a future date
3. New portfolios on Render can be invisible if only `transactions.csv` is copied to the persistent disk

### Bug #1: `_fetch_current_prices()` requests data for a future date

**Status:** Fix ready (stashed), not yet applied

**Symptom:** After market close on the US west coast, the app fails to load with yfinance errors: `"Invalid input - start date cannot be after end date"` and all tickers reported as "possibly delisted."

**Root Cause:** `_fetch_current_prices()` uses `yf.download(tickers, period="1d")`. yfinance interprets `period="1d"` relative to UTC. After ~4 PM PDT (midnight UTC), the calculated 1-day window starts on the next UTC day, which is in the future relative to market data, causing the start date to be after the end date.

**Fix:** Replace `period="1d"` with an explicit date range anchored to Eastern Time (market timezone). Fetch the last 5 calendar days (to cover weekends/holidays) and take the most recent close:
```python
end = pd.Timestamp.now(tz="America/New_York").normalize() + pd.Timedelta(days=1)
start = end - pd.Timedelta(days=5)
data = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                   end=end.strftime("%Y-%m-%d"), progress=False)
```

**Location:** `portfolio_engine.py`, `_fetch_current_prices()` function

**Fix stored in:** `git stash` on `main` branch

### Bug #3: New portfolios on Render can be invisible if only `transactions.csv` is copied to the persistent disk

**Status:** Fixed in `d65583b` (`Sync new portfolio configs during init`)

**Symptom:** Production deploy logs showed `Updated /data/portfolios/crypto_portfolio/transactions.csv`, but did not show `Crypto Portfolio: synced ... transactions`. The app deployed successfully, but the new portfolio was not discovered or synced.

**Root Cause:** Render keeps `/data` as a persistent disk across deploys. On an existing disk, `init_data.py` only copied `transactions.csv` from the repo. New portfolio discovery depends on `config.json`, so a portfolio directory with only `transactions.csv` is invisible to `portfolio_engine.list_portfolios()`.

**Fix:** `init_data.sync_transaction_files()` now copies repo-defined portfolio definition files, including `transactions.csv` and `config.json`, into `/data/portfolios`. The regression test now asserts both files are copied for a new portfolio.

**Lesson Learned:** Treat repo portfolio directories as a small schema, not as a single transactions file. When adding a new portfolio or changing portfolio metadata, deploy initialization must sync every file required for discovery and bootstrapping. Tests should assert the downstream discovery contract, not just that one copied file exists.

**Location:** `init_data.py`, `sync_transaction_files()` and `tests/test_init_data.py`
