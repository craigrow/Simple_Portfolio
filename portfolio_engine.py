import os
import csv
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd

BASE_DIR = os.path.dirname(__file__)
PORTFOLIOS_DIR = os.environ.get("PORTFOLIOS_DIR", os.path.join(BASE_DIR, "portfolios"))

COLUMNS = ["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"]


def get_paths(portfolio_id):
    """Return a dict of all file paths for a given portfolio."""
    root = os.path.join(PORTFOLIOS_DIR, portfolio_id)
    data = os.path.join(root, "data")
    return {
        "root": root,
        "data_dir": data,
        "transactions": os.path.join(root, "transactions.csv"),
        "portfolio": os.path.join(data, "portfolio.csv"),
        "shadow_voo": os.path.join(data, "shadow_voo.csv"),
        "shadow_qqq": os.path.join(data, "shadow_qqq.csv"),
        "splits": os.path.join(data, "splits.csv"),
        "dividends": os.path.join(data, "dividends.csv"),
        "price_history": os.path.join(data, "price_history.csv"),
        "last_updated": os.path.join(data, "last_updated.txt"),
        "config": os.path.join(root, "config.json"),
    }


def list_portfolios():
    """Return list of (portfolio_id, display_name) tuples, sorted alphabetically."""
    if not os.path.isdir(PORTFOLIOS_DIR):
        return []
    result = []
    for name in sorted(os.listdir(PORTFOLIOS_DIR)):
        config_path = os.path.join(PORTFOLIOS_DIR, name, "config.json")
        if os.path.isfile(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            result.append((name, cfg.get("name", name)))
    return result


def _ensure_file(path):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(COLUMNS)


def read_csv(path):
    _ensure_file(path)
    return pd.read_csv(path)


def _get_closing_price(ticker, date_str):
    """Fetch closing price for ticker on a given date."""
    t = yf.Ticker(ticker)
    start = pd.Timestamp(date_str)
    end = start + pd.Timedelta(days=1)
    hist = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    if hist.empty:
        return None
    return round(float(hist["Close"].iloc[0]), 2)


def _build_shadow_row(date_str, shadow_ticker, total_value):
    price = _get_closing_price(shadow_ticker, date_str)
    if price is None:
        return None
    shares = round(total_value / price, 5)
    return [date_str, shadow_ticker, price, shares, round(price * shares, 2)]


def _append_rows(path, rows):
    _ensure_file(path)
    with open(path, "rb") as f:
        f.seek(0, 2)
        if f.tell() > 0:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                with open(path, "a") as fa:
                    fa.write("\n")
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


def get_new_transactions(paths):
    """Return rows from transactions.csv not yet in portfolio.csv."""
    txn = pd.read_csv(paths["transactions"])
    portfolio = read_csv(paths["portfolio"])
    if portfolio.empty:
        return txn
    processed_count = len(portfolio)
    return txn.iloc[processed_count:]


def sync(paths):
    """Process new transactions and update portfolio + shadow files."""
    new = get_new_transactions(paths)
    if new.empty:
        return 0

    portfolio_rows = []
    voo_rows = []
    qqq_rows = []

    for _, row in new.iterrows():
        date_str = str(row["DATE"])
        ticker = str(row["TICKER"])
        price = float(row["PURCHASE_PRICE"])
        shares = round(float(row["SHARES_PURCHASED"]), 5)
        total = round(price * shares, 2)

        portfolio_rows.append([date_str, ticker, price, shares, total])

        voo_row = _build_shadow_row(date_str, "VOO", total)
        if voo_row:
            voo_rows.append(voo_row)

        qqq_row = _build_shadow_row(date_str, "QQQ", total)
        if qqq_row:
            qqq_rows.append(qqq_row)

    _append_rows(paths["portfolio"], portfolio_rows)
    _append_rows(paths["shadow_voo"], voo_rows)
    _append_rows(paths["shadow_qqq"], qqq_rows)

    return len(portfolio_rows)


def load_all(paths):
    """Return all three portfolios as DataFrames."""
    return (
        read_csv(paths["portfolio"]),
        read_csv(paths["shadow_voo"]),
        read_csv(paths["shadow_qqq"]),
    )


def _last_market_close():
    """Return the date of the most recent market close, using yfinance data.
    Accounts for weekends and holidays automatically."""
    data = yf.download("VOO", period="5d", progress=False)
    if not data.empty:
        last_date = data.index[-1]
        if hasattr(last_date, 'date'):
            return last_date.date()
        return pd.Timestamp(last_date).date()
    # Fallback: skip weekends only
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    candidate = now.date() if now.hour >= 16 else (now - timedelta(days=1)).date()
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _get_all_tickers(paths):
    """Return set of all tickers across all three portfolios."""
    tickers = set()
    for path in [paths["portfolio"], paths["shadow_voo"], paths["shadow_qqq"]]:
        df = read_csv(path)
        if not df.empty:
            tickers.update(df["TICKER"].unique())
    return tickers


def _fetch_splits(tickers):
    """Fetch split history for a set of tickers."""
    rows = []
    for ticker in tickers:
        t = yf.Ticker(ticker)
        splits = t.splits
        if splits is not None and len(splits) > 0:
            for date, ratio in splits.items():
                date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
                rows.append([ticker, date_str, float(ratio)])
    return rows


def sync_splits(paths):
    """Refresh split data if stale."""
    last_close = _last_market_close()

    if os.path.exists(paths["splits"]):
        mtime = datetime.fromtimestamp(
            os.path.getmtime(paths["splits"]), tz=ZoneInfo("America/New_York")
        )
        if mtime.date() >= last_close:
            return False

    tickers = _get_all_tickers(paths)
    if not tickers:
        return False

    rows = _fetch_splits(tickers)
    os.makedirs(os.path.dirname(paths["splits"]), exist_ok=True)
    with open(paths["splits"], "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["TICKER", "DATE", "RATIO"])
        for row in rows:
            writer.writerow(row)

    return True


def _fetch_dividends(tickers):
    """Fetch dividend history for a set of tickers."""
    rows = []
    for ticker in tickers:
        t = yf.Ticker(ticker)
        divs = t.dividends
        if divs is not None and len(divs) > 0:
            for date, amount in divs.items():
                date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
                rows.append([ticker, date_str, round(float(amount), 6)])
    return rows


def sync_dividends(paths):
    """Refresh dividend data if stale."""
    last_close = _last_market_close()

    if os.path.exists(paths["dividends"]):
        mtime = datetime.fromtimestamp(
            os.path.getmtime(paths["dividends"]), tz=ZoneInfo("America/New_York")
        )
        if mtime.date() >= last_close:
            return False

    tickers = _get_all_tickers(paths)
    if not tickers:
        return False

    rows = _fetch_dividends(tickers)
    os.makedirs(os.path.dirname(paths["dividends"]), exist_ok=True)
    with open(paths["dividends"], "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["TICKER", "DATE", "AMOUNT"])
        for row in rows:
            writer.writerow(row)

    return True


def _read_dividends(paths):
    """Read dividends.csv into a DataFrame."""
    if not os.path.exists(paths["dividends"]):
        return pd.DataFrame(columns=["TICKER", "DATE", "AMOUNT"])
    return pd.read_csv(paths["dividends"])


def get_total_dividends(ticker, shares, purchase_date, splits_df, dividends_df):
    """Calculate total dividends received for a holding since purchase date."""
    if dividends_df.empty:
        return 0.0
    if not isinstance(purchase_date, pd.Timestamp):
        purchase_date = pd.to_datetime(purchase_date)
    td = dividends_df[dividends_df["TICKER"] == ticker]
    if td.empty:
        return 0.0
    div_dates = td["_date"] if "_date" in td.columns else td["DATE"].apply(pd.to_datetime)
    td = td[div_dates > purchase_date]
    if td.empty:
        return 0.0
    # For each dividend, get split-adjusted shares at that point
    total = 0.0
    adj = get_adjusted_shares(ticker, shares, purchase_date, splits_df)
    total = (adj * td["AMOUNT"]).sum()
    return round(total, 2)


def _read_splits(paths):
    """Read splits.csv into a DataFrame."""
    if not os.path.exists(paths["splits"]):
        return pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
    return pd.read_csv(paths["splits"])


def get_adjusted_shares(ticker, shares, purchase_date, splits_df):
    """Apply all splits for ticker that occurred after purchase_date."""
    if splits_df.empty:
        return shares
    if not isinstance(purchase_date, pd.Timestamp):
        purchase_date = pd.to_datetime(purchase_date)
    ts = splits_df[splits_df["TICKER"] == ticker]
    if ts.empty:
        return shares
    mask = ts["_date"] > purchase_date if "_date" in ts.columns else ts["DATE"].apply(pd.to_datetime) > purchase_date
    ratio = ts.loc[mask, "RATIO"].prod()
    return round(shares * ratio, 5) if ratio != 0 else shares


def enrich_portfolio(portfolio_df, splits_df=None, dividends_df=None, current_prices=None, paths=None):
    """Add CURRENT_SHARES, CURRENT_VALUE, and TOTAL_DIVIDENDS columns."""
    if portfolio_df.empty:
        return portfolio_df, 0.0, 0.0

    if splits_df is None:
        splits_df = _read_splits(paths) if paths else pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
    if dividends_df is None:
        dividends_df = _read_dividends(paths) if paths else pd.DataFrame(columns=["TICKER", "DATE", "AMOUNT"])
    if current_prices is None:
        tickers = portfolio_df["TICKER"].unique().tolist()
        current_prices = _fetch_current_prices(tickers)

    current_shares = []
    current_values = []
    total_dividends = []

    # Pre-parse dates once to avoid repeated pd.to_datetime in loops
    if not splits_df.empty and "_date" not in splits_df.columns:
        splits_df = splits_df.copy()
        splits_df["_date"] = pd.to_datetime(splits_df["DATE"])
    if not dividends_df.empty and "_date" not in dividends_df.columns:
        dividends_df = dividends_df.copy()
        dividends_df["_date"] = pd.to_datetime(dividends_df["DATE"])

    for _, row in portfolio_df.iterrows():
        ticker = row["TICKER"]
        adj = get_adjusted_shares(ticker, row["SHARES_PURCHASED"], row["DATE"], splits_df)
        price = current_prices.get(ticker, 0.0)
        divs = get_total_dividends(ticker, row["SHARES_PURCHASED"], row["DATE"], splits_df, dividends_df)
        current_shares.append(adj)
        current_values.append(round(adj * price, 2))
        total_dividends.append(divs)

    enriched = portfolio_df.copy()
    enriched["CURRENT_SHARES"] = current_shares
    enriched["CURRENT_VALUE"] = current_values
    enriched["TOTAL_DIVIDENDS"] = total_dividends
    return enriched, round(sum(current_values), 2), round(sum(total_dividends), 2)


def portfolio_summary(enriched_df):
    """Aggregate per-transaction data into per-ticker summary for portfolio view."""
    if enriched_df.empty:
        return []
    g = enriched_df.groupby("TICKER").agg(
        SHARES_OWNED=("CURRENT_SHARES", "sum"),
        COST_BASIS=("TOTAL_VALUE", "sum"),
        CURRENT_VALUE=("CURRENT_VALUE", "sum"),
        DIVIDENDS=("TOTAL_DIVIDENDS", "sum"),
    ).reset_index()
    g["GAIN_LOSS"] = g["CURRENT_VALUE"] - g["COST_BASIS"]
    g = g.sort_values("CURRENT_VALUE", ascending=False).reset_index(drop=True)
    # Round
    for col in ["SHARES_OWNED", "COST_BASIS", "CURRENT_VALUE", "DIVIDENDS", "GAIN_LOSS"]:
        g[col] = g[col].round(2)
    return g.to_dict("records")


def _fetch_current_prices(tickers):
    """Fetch current prices for a list of tickers."""
    if not tickers:
        return {}
    end = pd.Timestamp.now(tz="America/New_York").normalize() + pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=5)  # 5 days back to cover weekends/holidays
    data = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                       end=end.strftime("%Y-%m-%d"), progress=False)
    prices = {}
    if len(tickers) == 1:
        if not data.empty:
            prices[tickers[0]] = round(float(data["Close"].iloc[-1].iloc[0]), 2)
    else:
        for ticker in tickers:
            try:
                prices[ticker] = round(float(data["Close"][ticker].iloc[-1]), 2)
            except (KeyError, IndexError):
                pass
    return prices


def fetch_all_history(portfolios, splits_df, dividends_df, paths):
    """Fetch historical closing prices, using cache where possible."""
    all_tickers = set()
    earliest = None
    for df in portfolios:
        if df.empty:
            continue
        all_tickers.update(df["TICKER"].unique())
        first = pd.to_datetime(df["DATE"]).min()
        if earliest is None or first < earliest:
            earliest = first
    if not all_tickers or earliest is None:
        return pd.DataFrame()

    needed = sorted(all_tickers)

    cached = None
    if os.path.exists(paths["price_history"]):
        cached = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)

    last_close = _last_market_close()
    new_tickers = []
    existing_tickers = []
    for t in needed:
        if cached is None or t not in cached.columns:
            new_tickers.append(t)
        else:
            first_valid = cached[t].first_valid_index()
            if first_valid is None or first_valid > earliest + pd.Timedelta(days=7):
                new_tickers.append(t)
            else:
                existing_tickers.append(t)

    frames = []

    if new_tickers:
        data = yf.download(new_tickers, start=earliest.strftime("%Y-%m-%d"), progress=False)["Close"]
        if isinstance(data, pd.Series):
            data = data.to_frame(name=new_tickers[0])
        frames.append(data)
        if cached is not None:
            cached = cached.drop(columns=[t for t in new_tickers if t in cached.columns], errors="ignore")

    if existing_tickers and cached is not None:
        last_cached = cached.index.max()
        if last_cached.tzinfo is None:
            last_cached = last_cached.tz_localize("America/New_York")
        if last_cached.date() < last_close:
            start = (last_cached + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            delta = yf.download(existing_tickers, start=start, progress=False)
            if not delta.empty:
                delta_close = delta["Close"]
                if isinstance(delta_close, pd.Series):
                    delta_close = delta_close.to_frame(name=existing_tickers[0])
                existing_cached = cached[existing_tickers]
                frames.append(pd.concat([existing_cached, delta_close]))
            else:
                frames.append(cached[existing_tickers])
        else:
            frames.append(cached[existing_tickers])

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, axis=1)
    result = result[~result.index.duplicated(keep="last")].sort_index()

    os.makedirs(os.path.dirname(paths["price_history"]), exist_ok=True)
    result.to_csv(paths["price_history"])

    return result


def get_historical_values(portfolio_df, splits_df, dividends_df, prices_df):
    """Return a list of dicts with DATE and VALUE for each trading day."""
    if portfolio_df.empty or prices_df.empty:
        return []

    dates = prices_df.index
    totals = pd.Series(0.0, index=dates)

    for _, row in portfolio_df.iterrows():
        ticker = row["TICKER"]
        purchase_dt = pd.to_datetime(row["DATE"])
        shares = float(row["SHARES_PURCHASED"])

        if ticker not in prices_df.columns:
            continue

        adj = get_adjusted_shares(ticker, shares, row["DATE"], splits_df)

        price_series = prices_df[ticker].fillna(0.0)
        mask = dates >= purchase_dt
        totals += price_series * adj * mask

        if not dividends_df.empty:
            ticker_divs = dividends_df[
                (dividends_df["TICKER"] == ticker) &
                (dividends_df["DATE"].apply(lambda d: pd.to_datetime(d) > purchase_dt))
            ]
            for _, div in ticker_divs.iterrows():
                div_dt = pd.to_datetime(div["DATE"])
                div_adj = get_adjusted_shares(ticker, shares, row["DATE"], splits_df)
                div_amount = div_adj * div["AMOUNT"]
                totals += div_amount * (dates >= div_dt)

    return [{"DATE": d.strftime("%Y-%m-%d"), "VALUE": round(v, 2)}
            for d, v in totals.items()]


def get_last_updated(paths):
    """Read the last_updated date and return a display string."""
    if os.path.exists(paths["last_updated"]):
        with open(paths["last_updated"]) as f:
            raw = f.read().strip()
        try:
            dt = pd.to_datetime(raw)
            return dt.strftime("%B %-d, %Y") + " 4:00 PM ET"
        except Exception:
            return raw
    return None


def needs_refresh(paths):
    """Return True if cached data doesn't cover the most recent market close."""
    if not os.path.exists(paths["last_updated"]):
        return True
    try:
        with open(paths["last_updated"]) as f:
            raw = f.read().strip()
        updated_date = pd.to_datetime(raw).date()
        return updated_date < _last_market_close()
    except Exception:
        return True


def _set_last_updated(paths, close_date):
    """Write market close date to last_updated file (ISO format)."""
    os.makedirs(os.path.dirname(paths["last_updated"]), exist_ok=True)
    with open(paths["last_updated"], "w") as f:
        f.write(str(close_date))
    return str(close_date)


def update_prices(paths, max_retries=3):
    """Fetch prices for all tickers with retries, update cache, record timestamp.
    Returns dict with status and any failed tickers."""
    import time

    all_tickers = sorted(_get_all_tickers(paths))
    if not all_tickers:
        close_date = _last_market_close()
        return {"status": "ok", "last_updated": _set_last_updated(paths, close_date)}

    close_date = _last_market_close()

    # Load existing cache
    if os.path.exists(paths["price_history"]):
        raw = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
        cached = raw[~raw.index.duplicated(keep="last")]
        if len(cached) < len(raw):
            cached.to_csv(paths["price_history"])
    else:
        cached = pd.DataFrame()

    # Determine start date for fetch
    port_df = read_csv(paths["portfolio"])
    earliest = pd.to_datetime(port_df["DATE"]).min() if not port_df.empty else None
    if cached is not None and not cached.empty:
        start = (cached.index.max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if cached.index.max().date() >= close_date:
            return {"status": "ok", "last_updated": _set_last_updated(paths, close_date)}
    elif earliest is not None:
        start = earliest.strftime("%Y-%m-%d")
    else:
        return {"status": "ok", "last_updated": _set_last_updated(paths, close_date)}

    # Fetch with retries for missing tickers
    remaining = list(all_tickers)
    new_frames = []

    for attempt in range(max_retries):
        if not remaining:
            break
        try:
            data = yf.download(remaining, start=start, progress=False)
            if data.empty:
                break
            close = data["Close"]
            if isinstance(close, pd.Series):
                close = close.to_frame(name=remaining[0])
            new_frames.append(close)
            # Check which tickers got data for the close_date
            got = [t for t in remaining if t in close.columns
                   and close[t].last_valid_index() is not None
                   and close[t].last_valid_index().date() >= close_date]
            remaining = [t for t in remaining if t not in got]
        except Exception:
            pass
        if remaining and attempt < max_retries - 1:
            time.sleep(3)

    # Merge new data into cache
    if new_frames:
        new_data = pd.concat(new_frames, axis=1)
        new_data = new_data.loc[:, ~new_data.columns.duplicated(keep="last")]
        new_data = new_data[~new_data.index.duplicated(keep="last")]
        if not new_data.empty:
            if not cached.empty:
                combined = pd.concat([cached, new_data])
                combined = combined.loc[:, ~combined.columns.duplicated(keep="last")]
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            else:
                combined = new_data.sort_index()
            os.makedirs(os.path.dirname(paths["price_history"]), exist_ok=True)
            combined.to_csv(paths["price_history"])

    if remaining:
        return {"status": "incomplete", "failed_tickers": remaining}

    ts = _set_last_updated(paths, close_date)
    return {"status": "ok", "last_updated": ts}


def refresh_data(paths):
    """Full refresh: sync new transactions, splits, dividends, and update prices."""
    sync(paths)
    try:
        sync_splits(paths)
    except Exception:
        pass
    try:
        sync_dividends(paths)
    except Exception:
        pass
    return update_prices(paths)
