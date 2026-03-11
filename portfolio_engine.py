import os
import csv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TRANSACTIONS_FILE = os.path.join(os.path.dirname(__file__), "transactions.csv")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.csv")
SHADOW_VOO_FILE = os.path.join(DATA_DIR, "shadow_voo.csv")
SHADOW_QQQ_FILE = os.path.join(DATA_DIR, "shadow_qqq.csv")
SPLITS_FILE = os.path.join(DATA_DIR, "splits.csv")
DIVIDENDS_FILE = os.path.join(DATA_DIR, "dividends.csv")

COLUMNS = ["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"]


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
    # Ensure file ends with a newline before appending
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


def get_new_transactions():
    """Return rows from transactions.csv not yet in portfolio.csv."""
    txn = pd.read_csv(TRANSACTIONS_FILE)
    portfolio = read_csv(PORTFOLIO_FILE)

    if portfolio.empty:
        return txn
    # Compare by row count — new transactions are appended at the end
    processed_count = len(portfolio)
    return txn.iloc[processed_count:]


def sync():
    """Process new transactions and update portfolio + shadow files."""
    new = get_new_transactions()
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

    _append_rows(PORTFOLIO_FILE, portfolio_rows)
    _append_rows(SHADOW_VOO_FILE, voo_rows)
    _append_rows(SHADOW_QQQ_FILE, qqq_rows)

    return len(portfolio_rows)


def load_all():
    """Return all three portfolios as DataFrames."""
    return (
        read_csv(PORTFOLIO_FILE),
        read_csv(SHADOW_VOO_FILE),
        read_csv(SHADOW_QQQ_FILE),
    )


def _last_market_close():
    """Return the datetime of the most recent 4 PM ET market close."""
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    close_today = now.replace(hour=16, minute=0, second=0, microsecond=0)

    # If it's before today's close, look at previous days
    if now < close_today:
        candidate = close_today - timedelta(days=1)
    else:
        candidate = close_today

    # Walk back to a weekday (Mon-Fri)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate -= timedelta(days=1)

    return candidate


def _get_all_tickers():
    """Return set of all tickers across all three portfolios."""
    tickers = set()
    for path in [PORTFOLIO_FILE, SHADOW_VOO_FILE, SHADOW_QQQ_FILE]:
        df = read_csv(path)
        if not df.empty:
            tickers.update(df["TICKER"].unique())
    return tickers


def _fetch_splits(tickers):
    """Fetch split history for a set of tickers. Returns list of [TICKER, DATE, RATIO] rows."""
    rows = []
    for ticker in tickers:
        t = yf.Ticker(ticker)
        splits = t.splits
        if splits is not None and len(splits) > 0:
            for date, ratio in splits.items():
                date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
                rows.append([ticker, date_str, float(ratio)])
    return rows


def sync_splits():
    """Refresh split data if stale (last modified before most recent market close)."""
    last_close = _last_market_close()

    if os.path.exists(SPLITS_FILE):
        mtime = datetime.fromtimestamp(
            os.path.getmtime(SPLITS_FILE), tz=ZoneInfo("America/New_York")
        )
        if mtime >= last_close:
            return False  # Still fresh

    tickers = _get_all_tickers()
    if not tickers:
        return False

    rows = _fetch_splits(tickers)
    os.makedirs(os.path.dirname(SPLITS_FILE), exist_ok=True)
    with open(SPLITS_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["TICKER", "DATE", "RATIO"])
        for row in rows:
            writer.writerow(row)

    return True


def _fetch_dividends(tickers):
    """Fetch dividend history for a set of tickers. Returns list of [TICKER, DATE, AMOUNT] rows."""
    rows = []
    for ticker in tickers:
        t = yf.Ticker(ticker)
        divs = t.dividends
        if divs is not None and len(divs) > 0:
            for date, amount in divs.items():
                date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
                rows.append([ticker, date_str, round(float(amount), 6)])
    return rows


def sync_dividends():
    """Refresh dividend data if stale (last modified before most recent market close)."""
    last_close = _last_market_close()

    if os.path.exists(DIVIDENDS_FILE):
        mtime = datetime.fromtimestamp(
            os.path.getmtime(DIVIDENDS_FILE), tz=ZoneInfo("America/New_York")
        )
        if mtime >= last_close:
            return False

    tickers = _get_all_tickers()
    if not tickers:
        return False

    rows = _fetch_dividends(tickers)
    os.makedirs(os.path.dirname(DIVIDENDS_FILE), exist_ok=True)
    with open(DIVIDENDS_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["TICKER", "DATE", "AMOUNT"])
        for row in rows:
            writer.writerow(row)

    return True


def _read_dividends():
    """Read dividends.csv into a DataFrame."""
    if not os.path.exists(DIVIDENDS_FILE):
        return pd.DataFrame(columns=["TICKER", "DATE", "AMOUNT"])
    return pd.read_csv(DIVIDENDS_FILE)


def get_total_dividends(ticker, shares, purchase_date, splits_df, dividends_df):
    """Calculate total dividends received for a holding since purchase date."""
    if dividends_df.empty:
        return 0.0
    purchase_dt = pd.to_datetime(purchase_date)
    ticker_divs = dividends_df[dividends_df["TICKER"] == ticker].copy()
    ticker_divs = ticker_divs[ticker_divs["DATE"].apply(lambda d: pd.to_datetime(d) > purchase_dt)]
    total = 0.0
    for _, div in ticker_divs.iterrows():
        # Shares held at time of dividend = original shares adjusted for splits before dividend date
        div_dt = pd.to_datetime(div["DATE"])
        relevant_splits = splits_df[splits_df["DATE"].apply(lambda d: pd.to_datetime(d) <= div_dt)]
        adj_shares = get_adjusted_shares(ticker, shares, purchase_date, relevant_splits)
        total += adj_shares * div["AMOUNT"]
    return round(total, 2)


def _read_splits():
    """Read splits.csv into a DataFrame."""
    if not os.path.exists(SPLITS_FILE):
        return pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
    return pd.read_csv(SPLITS_FILE)


def get_adjusted_shares(ticker, shares, purchase_date, splits_df):
    """Apply all splits for ticker that occurred after purchase_date."""
    if splits_df.empty:
        return shares
    purchase_dt = pd.to_datetime(purchase_date)
    ticker_splits = splits_df[splits_df["TICKER"] == ticker]
    for _, split in ticker_splits.iterrows():
        if pd.to_datetime(split["DATE"]) > purchase_dt:
            shares *= split["RATIO"]
    return round(shares, 5)


def get_current_values(portfolio_df):
    """Calculate total current value of a portfolio, adjusted for splits."""
    if portfolio_df.empty:
        return 0.0

    splits_df = _read_splits()
    tickers = portfolio_df["TICKER"].unique().tolist()

    # Batch fetch current prices
    current_prices = _fetch_current_prices(tickers)

    total = 0.0
    for _, row in portfolio_df.iterrows():
        ticker = row["TICKER"]
        shares = get_adjusted_shares(
            ticker, row["SHARES_PURCHASED"], row["DATE"], splits_df
        )
        price = current_prices.get(ticker, 0.0)
        total += shares * price

    return round(total, 2)


def enrich_portfolio(portfolio_df, splits_df=None, dividends_df=None, current_prices=None):
    """Add CURRENT_SHARES, CURRENT_VALUE, and TOTAL_DIVIDENDS columns."""
    if portfolio_df.empty:
        return portfolio_df, 0.0, 0.0

    if splits_df is None:
        splits_df = _read_splits()
    if dividends_df is None:
        dividends_df = _read_dividends()
    if current_prices is None:
        tickers = portfolio_df["TICKER"].unique().tolist()
        current_prices = _fetch_current_prices(tickers)

    current_shares = []
    current_values = []
    total_dividends = []
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


def _fetch_current_prices(tickers):
    """Fetch current prices for a list of tickers. Returns dict of ticker -> price."""
    if not tickers:
        return {}
    data = yf.download(tickers, period="1d", progress=False)
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


PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.csv")


def fetch_all_history(portfolios, splits_df, dividends_df):
    """Fetch historical closing prices, using cache where possible.

    Returns a DataFrame of closing prices indexed by date, with one column per ticker.
    """
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

    # Load cache
    cached = None
    if os.path.exists(PRICE_HISTORY_FILE):
        cached = pd.read_csv(PRICE_HISTORY_FILE, index_col=0, parse_dates=True)

    last_close = _last_market_close()
    new_tickers = []
    existing_tickers = []
    for t in needed:
        if cached is None or t not in cached.columns:
            new_tickers.append(t)
        else:
            # If cached data for this ticker starts much later than earliest purchase, refetch
            first_valid = cached[t].first_valid_index()
            if first_valid is None or first_valid > earliest + pd.Timedelta(days=7):
                new_tickers.append(t)
            else:
                existing_tickers.append(t)

    frames = []

    # Full fetch for new tickers (or tickers with incomplete cache)
    if new_tickers:
        data = yf.download(new_tickers, start=earliest.strftime("%Y-%m-%d"), progress=False)["Close"]
        if isinstance(data, pd.Series):
            data = data.to_frame(name=new_tickers[0])
        frames.append(data)
        # Drop these columns from cache if they existed with bad data
        if cached is not None:
            cached = cached.drop(columns=[t for t in new_tickers if t in cached.columns], errors="ignore")

    # Delta fetch for existing tickers if stale
    if existing_tickers and cached is not None:
        last_cached = cached.index.max()
        if last_cached.tzinfo is None:
            last_cached = last_cached.tz_localize("America/New_York")
        if last_cached < last_close:
            start = (last_cached + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            delta = yf.download(existing_tickers, start=start, progress=False)
            if not delta.empty:
                delta_close = delta["Close"]
                if isinstance(delta_close, pd.Series):
                    delta_close = delta_close.to_frame(name=existing_tickers[0])
                # Append new rows to cached existing columns
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

    # Save cache
    os.makedirs(DATA_DIR, exist_ok=True)
    result.to_csv(PRICE_HISTORY_FILE)

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

        # Get split-adjusted share count (same for all dates after last split)
        adj = get_adjusted_shares(ticker, shares, row["DATE"], splits_df)

        # Price series for this ticker, zero before purchase
        price_series = prices_df[ticker].fillna(0.0)
        mask = dates >= purchase_dt
        totals += price_series * adj * mask

        # Add cumulative dividends
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
