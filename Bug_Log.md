## Bug Log

1. `_fetch_current_prices()` requests data for a future date
2. ~~Portfolio current values appear to be wrong. Foolish Portfolio shows almost no growth and VOO shows curent value of $0.~~ Root cause is #1, `_fetch_current_prices()` requests data for a future date

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