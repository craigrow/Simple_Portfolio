import pricing_sources
import split_history
import os
import csv
import json
import hashlib
import tempfile
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd
import portfolio_replay

BASE_DIR = os.path.dirname(__file__)
PORTFOLIOS_DIR = os.environ.get("PORTFOLIOS_DIR", os.path.join(BASE_DIR, "portfolios"))

COLUMNS = ["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"]
MANUAL_PRICE_COLUMNS = ["DATE", "TICKER", "PRICE"]
MANUAL_DIVIDEND_COLUMNS = ["DATE", "TICKER", "AMOUNT"]
DEFAULT_PORTFOLIO_ID = "foolish_portfolio"
BENCHMARK_SHADOW_PATHS = {
    "VOO": "shadow_voo",
    "QQQ": "shadow_qqq",
}
PRICE_POLICY = "unadjusted-close-v1"


def get_paths(portfolio_id):
    """Return a dict of all file paths for a given portfolio."""
    root = os.path.join(PORTFOLIOS_DIR, portfolio_id)
    data = os.path.join(root, "data")
    return {
        "root": root,
        "data_dir": data,
        "transactions": os.path.join(root, "transactions.csv"),
        "manual_prices": os.path.join(root, "manual_prices.csv"),
        "manual_dividends": os.path.join(root, "dividends_received.csv"),
        "portfolio": os.path.join(data, "portfolio.csv"),
        "shadow_voo": os.path.join(data, "shadow_voo.csv"),
        "shadow_qqq": os.path.join(data, "shadow_qqq.csv"),
        "splits": os.path.join(data, "splits.csv"),
        "split_status": os.path.join(data, "split_status.json"),
        "split_attempts": os.path.join(data, "split_attempts.jsonl"),
        "dividends": os.path.join(data, "dividends.csv"),
        "price_history": os.path.join(data, "price_history.csv"),
        "provisional_prices": os.path.join(data, "price_history.provisional"),
        "price_policy": os.path.join(data, "price_history.policy"),
        "price_kinds": os.path.join(data, "price_kinds.json"),
        "price_receipts": os.path.join(data, "price_receipts.json"),
        "last_updated": os.path.join(data, "last_updated.txt"),
        "daily_values": os.path.join(data, "daily_values.csv"),
        "accounting_snapshot": os.path.join(data, "accounting_snapshot.json"),
        "config": os.path.join(root, "config.json"),
    }


def list_portfolios():
    """Return list of (portfolio_id, display_name) tuples, with the default first."""
    if not os.path.isdir(PORTFOLIOS_DIR):
        return []
    result = []
    portfolio_ids = sorted(os.listdir(PORTFOLIOS_DIR), key=lambda name: (name != DEFAULT_PORTFOLIO_ID, name))
    for name in portfolio_ids:
        config_path = os.path.join(PORTFOLIOS_DIR, name, "config.json")
        if os.path.isfile(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            result.append((name, cfg.get("name", name)))
    return result


def _load_config(paths):
    """Return the portfolio's config dict, or empty dict if missing/unreadable."""
    if not os.path.exists(paths["config"]):
        return {}
    try:
        with open(paths["config"]) as f:
            return json.load(f)
    except Exception:
        return {}


def is_manual_pricing(paths):
    """Return True if this portfolio's prices are entered manually (no yfinance)."""
    return bool(_load_config(paths).get("manual_pricing"))


def _read_manual_prices(paths):
    """Read manual_prices.csv into a DataFrame (empty if the file is absent)."""
    if not os.path.exists(paths["manual_prices"]):
        return pd.DataFrame(columns=MANUAL_PRICE_COLUMNS)
    df = pd.read_csv(paths["manual_prices"])
    for col in MANUAL_PRICE_COLUMNS:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df[MANUAL_PRICE_COLUMNS]


def _manual_tickers(paths):
    """Instrument acquisition modes; legacy portfolio flags are migrated by the adapter."""
    return {ticker for ticker, mode in pricing_sources.instrument_modes(paths).items() if mode == "static"}


def _manual_price_points(paths):
    """Return a long DataFrame [DATE, TICKER, PRICE] of all manual price points.

    Combines transaction purchase prices (each buy is a price observation) with
    explicit entries in manual_prices.csv. Explicit entries win on conflict."""
    frames = []
    if os.path.exists(paths["transactions"]):
        txn = pd.read_csv(paths["transactions"])
        if not txn.empty and {"DATE", "TICKER", "PURCHASE_PRICE"}.issubset(txn.columns):
            t = txn[["DATE", "TICKER", "PURCHASE_PRICE"]].dropna(subset=["TICKER"]).copy()
            t.columns = MANUAL_PRICE_COLUMNS
            frames.append(t)
        elif not txn.empty and {"DATE", "TICKER", "PRICE"}.issubset(txn.columns):
            t = txn[["DATE", "TICKER", "PRICE"]].dropna(subset=["TICKER"]).copy()
            t.columns = MANUAL_PRICE_COLUMNS
            frames.append(t)
    manual = _read_manual_prices(paths)
    if not manual.empty:
        frames.append(manual)
    if not frames:
        return pd.DataFrame(columns=MANUAL_PRICE_COLUMNS)
    combined = pd.concat(frames, ignore_index=True)
    combined["DATE"] = combined["DATE"].astype(str)
    combined["TICKER"] = combined["TICKER"].astype(str)
    combined["PRICE"] = combined["PRICE"].astype(float)
    # Explicit manual entries appear last, so keep="last" lets them win.
    combined = combined.drop_duplicates(subset=["DATE", "TICKER"], keep="last")
    return combined.reset_index(drop=True)


def _read_manual_dividends(paths):
    """Read the lump-sum dividends_received.csv (empty if absent).

    Unlike yfinance dividends (per-share amounts), these are total dollars
    received for a holding on a date — the form brokerages like Fundrise report."""
    if not os.path.exists(paths["manual_dividends"]):
        return pd.DataFrame(columns=MANUAL_DIVIDEND_COLUMNS)
    df = pd.read_csv(paths["manual_dividends"])
    for col in MANUAL_DIVIDEND_COLUMNS:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df[MANUAL_DIVIDEND_COLUMNS]


def manual_dividends_by_ticker(paths):
    """Return {ticker: total_dollars_received} from dividends_received.csv."""
    df = _read_manual_dividends(paths)
    if df.empty:
        return {}
    df = df.copy()
    df["AMOUNT"] = df["AMOUNT"].astype(float)
    return df.groupby(df["TICKER"].astype(str))["AMOUNT"].sum().round(2).to_dict()


def add_manual_dividend(paths, date_str, ticker, amount):
    """Append a lump-sum dividend received for a ticker on a date. Returns the row."""
    df = _read_manual_dividends(paths)
    date_str = str(date_str)
    ticker = str(ticker)
    amount = round(float(amount), 2)
    new_row = pd.DataFrame([[date_str, ticker, amount]], columns=MANUAL_DIVIDEND_COLUMNS)
    df = pd.concat([df, new_row], ignore_index=True)
    df = df.sort_values(["DATE", "TICKER"]).reset_index(drop=True)
    df.to_csv(paths["manual_dividends"], index=False)
    return [date_str, ticker, amount]


def add_manual_price(paths, date_str, ticker, price):
    """Append (or update) a manual price for a ticker on a date. Returns the row.

    Manual prices are the source of truth for manually-priced holdings; they are
    tracked in git and merged into price_history.csv on refresh."""
    df = _read_manual_prices(paths)
    date_str = str(date_str)
    ticker = str(ticker)
    price = float(price)
    if not (0 < price < float("inf")):
        raise ValueError("Manual price must be finite and positive")
    mask = (df["DATE"].astype(str) == date_str) & (df["TICKER"].astype(str) == ticker)
    df = df[~mask]
    new_row = pd.DataFrame([[date_str, ticker, price]], columns=MANUAL_PRICE_COLUMNS)
    df = pd.concat([df, new_row], ignore_index=True)
    df = df.sort_values(["TICKER", "DATE"]).reset_index(drop=True)
    df.to_csv(paths["manual_prices"], index=False)
    return [date_str, ticker, price]


def sync_manual_prices(paths):
    """Maintain a compatibility CSV projection for static instruments.

    Valuation reads authoritative transaction/manual observations through the
    shared resolver. This projection remains for legacy reports and migration.
    """
    manual = _manual_price_points(paths)
    manual = manual[manual["TICKER"].isin(_manual_tickers(paths))]
    if manual.empty:
        return False

    manual = manual.copy()
    manual["DATE"] = pd.to_datetime(manual["DATE"])
    manual["PRICE"] = manual["PRICE"].astype(float)
    wide = manual.pivot_table(index="DATE", columns="TICKER", values="PRICE", aggfunc="last")
    wide.index.name = None

    if os.path.exists(paths["price_history"]):
        cached = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
    else:
        cached = pd.DataFrame()

    if cached.empty:
        combined = wide.sort_index()
    else:
        combined = cached.copy()
        for ticker in wide.columns:
            if ticker not in combined.columns:
                combined[ticker] = pd.NA
        # Manual values overwrite cached values for matching (date, ticker)
        combined = combined.combine_first(wide)  # union of index/columns
        for ticker in wide.columns:
            combined.loc[wide.index, ticker] = wide[ticker]
        combined = combined.sort_index()

    os.makedirs(os.path.dirname(paths["price_history"]), exist_ok=True)
    combined.to_csv(paths["price_history"])
    return True


def _ensure_file(path):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(COLUMNS)


def read_csv(path):
    _ensure_file(path)
    return pd.read_csv(path)


def _read_existing_csv(path, columns=None):
    """Read a CSV without creating it; used by read-only reporting paths."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=columns or [])
    return pd.read_csv(path)


def _get_closing_price(ticker, date_str):
    """Fetch closing price for ticker on a given date.
    Falls back to the most recent prior trading day if the market was closed."""
    t = yf.Ticker(ticker)
    start = pd.Timestamp(date_str)
    end = start + pd.Timedelta(days=1)
    hist = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    if hist.empty:
        lookback_start = start - pd.Timedelta(days=5)
        hist = t.history(start=lookback_start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"))
        if hist.empty:
            return None
    return round(float(hist["Close"].iloc[-1]), 2)


def _get_cached_closing_price(paths, ticker, date_str):
    """Return the cached close for ticker on date_str, or None if not covered.

    Falling back to the most recent prior close is only valid inside the range
    the cache actually spans: there the gap means the market was shut. Past the
    newest cached row the cache knows nothing about that date, so it must
    report a miss and let the caller fetch, rather than forward-filling a stale
    close onto a later day. Provisional rows hold intraday snapshots, not
    closes, so they cannot serve as a baseline either."""
    if not os.path.exists(paths["price_history"]):
        return None
    try:
        prices = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
    except Exception:
        return None
    if prices.empty or ticker not in prices.columns:
        return None

    provisional = _read_provisional_dates(paths)
    if provisional:
        keep = ~prices.index.normalize().strftime("%Y-%m-%d").isin(provisional)
        prices = prices.loc[keep]
        if prices.empty:
            return None

    settled = prices[ticker].dropna()
    if settled.empty:
        return None

    target = pd.Timestamp(date_str).normalize()
    if target > settled.index.normalize().max():
        return None

    prior = settled.loc[settled.index.normalize() <= target]
    if prior.empty:
        return None
    return round(float(prior.iloc[-1]), 2)


def _build_shadow_row(date_str, shadow_ticker, total_value, paths=None):
    price = _get_cached_closing_price(paths, shadow_ticker, date_str) if paths else None
    if price is None:
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


def _processed_rows_from_transactions(transactions):
    rows = []
    for _, row in transactions.iterrows():
        date_str = str(row["DATE"])
        ticker = str(row["TICKER"])
        price = float(row["PURCHASE_PRICE"])
        shares = round(float(row["SHARES_PURCHASED"]), 5)
        total = round(price * shares, 2)
        rows.append([date_str, ticker, price, shares, total])
    return pd.DataFrame(rows, columns=COLUMNS)


def _portfolio_rows_match_transactions(portfolio, expected):
    if len(portfolio) != len(expected):
        return False
    if portfolio.empty and expected.empty:
        return True
    current = portfolio[COLUMNS].copy()
    current["DATE"] = current["DATE"].astype(str)
    current["TICKER"] = current["TICKER"].astype(str)
    expected = expected.copy()
    expected["DATE"] = expected["DATE"].astype(str)
    expected["TICKER"] = expected["TICKER"].astype(str)
    for col in ["PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"]:
        current[col] = current[col].astype(float).round(5)
        expected[col] = expected[col].astype(float).round(5)
    return current.reset_index(drop=True).equals(expected.reset_index(drop=True))


def _invalidate_daily_values(paths):
    for path in [paths["daily_values"], paths["daily_values"] + ".meta"]:
        if os.path.exists(path):
            os.remove(path)


def _accounting_input_fingerprint(paths):
    """Return a stable identifier for every input that changes replay history."""
    digest = hashlib.sha256()
    digest.update(b"accounting-shared-pricing-v2\0")
    for key in (
        "transactions", "splits", "dividends", "manual_dividends", "manual_prices",
        "price_history", "provisional_prices", "price_policy", "price_kinds", "price_receipts", "config",
    ):
        path = paths.get(key)
        digest.update(key.encode("utf-8") + b"\0")
        if not path or not os.path.exists(path):
            digest.update(b"<missing>\0")
            continue
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _cached_accounting_fingerprint(paths):
    meta_path = paths["daily_values"] + ".meta"
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as meta_file:
            metadata = json.load(meta_file)
        if metadata.get("mode") == "accounting-v1":
            return metadata.get("input_sha256")
    except (OSError, ValueError, AttributeError):
        pass
    return None


def invalidate_daily_values(paths):
    """Public wrapper — force-clear the daily values cache for a portfolio."""
    _invalidate_daily_values(paths)


def invalidate_all_caches(paths):
    """Nuclear option — clear all cached data so everything is rebuilt."""
    _invalidate_daily_values(paths)
    for key in ["price_history", "provisional_prices", "splits", "portfolio",
                "shadow_voo", "shadow_qqq", "dividends", "accounting_snapshot",
                "price_policy"]:
        if paths.get(key) and os.path.exists(paths[key]):
            os.remove(paths[key])


def _shadow_rows_align_with_portfolio(portfolio, shadow):
    """Return the portfolio index matched by the final shadow row.

    Shadow portfolios may skip transactions from dates where the benchmark did
    not trade, so shadow rows must be an ordered subset rather than an exact
    row-for-row match.
    """
    if shadow.empty:
        return -1

    portfolio_rows = portfolio[["DATE", "TOTAL_VALUE"]].copy()
    shadow_rows = shadow[["DATE", "TOTAL_VALUE"]].copy()
    portfolio_rows["DATE"] = portfolio_rows["DATE"].astype(str)
    shadow_rows["DATE"] = shadow_rows["DATE"].astype(str)
    portfolio_rows["TOTAL_VALUE"] = portfolio_rows["TOTAL_VALUE"].astype(float).round(2)
    shadow_rows["TOTAL_VALUE"] = shadow_rows["TOTAL_VALUE"].astype(float).round(2)

    portfolio_idx = 0
    last_match = -1
    for _, shadow_row in shadow_rows.iterrows():
        found = False
        while portfolio_idx < len(portfolio_rows):
            portfolio_row = portfolio_rows.iloc[portfolio_idx]
            if (
                portfolio_row["DATE"] == shadow_row["DATE"]
                and portfolio_row["TOTAL_VALUE"] == shadow_row["TOTAL_VALUE"]
            ):
                last_match = portfolio_idx
                portfolio_idx += 1
                found = True
                break
            portfolio_idx += 1
        if not found:
            return None
    return last_match


def _repair_trailing_shadow_rows(paths, portfolio):
    """Append missing trailing shadow rows, or raise on non-trailing mismatches."""
    if portfolio.empty:
        return False

    repaired = False
    for shadow_key, shadow_ticker in [("shadow_voo", "VOO"), ("shadow_qqq", "QQQ")]:
        shadow = read_csv(paths[shadow_key])
        last_match = _shadow_rows_align_with_portfolio(portfolio, shadow)
        if last_match is None:
            raise RuntimeError(f"{shadow_ticker} shadow rows do not match portfolio transactions.")

        missing = portfolio.iloc[last_match + 1:]
        rows = []
        for _, row in missing.iterrows():
            shadow_row = _build_shadow_row(paths=paths, date_str=str(row["DATE"]),
                                           shadow_ticker=shadow_ticker,
                                           total_value=float(row["TOTAL_VALUE"]))
            if shadow_row is not None:
                rows.append(shadow_row)
        if rows:
            _append_rows(paths[shadow_key], rows)
            repaired = True

    if repaired:
        _invalidate_daily_values(paths)
    return repaired


def sync(paths):
    """Process new transactions and update portfolio + shadow files."""
    txn = pd.read_csv(paths["transactions"])
    if "ACTION" in txn.columns:
        # The versioned event ledger is replayed as a whole. Legacy positional
        # portfolio/shadow outputs must not be updated from BUY/SELL rows.
        portfolio_replay.normalize_transactions(txn)
        # Replay always rebuilds from inputs; retain the last chart until a
        # replacement succeeds so unavailable market data cannot erase it.
        return 0
    portfolio = read_csv(paths["portfolio"])
    if not portfolio.empty:
        processed_count = min(len(portfolio), len(txn))
        expected = _processed_rows_from_transactions(txn.iloc[:processed_count])
        current = portfolio.iloc[:processed_count]
        if len(portfolio) > len(txn) or not _portfolio_rows_match_transactions(current, expected):
            expected = _processed_rows_from_transactions(txn.iloc[:processed_count])
            expected.to_csv(paths["portfolio"], index=False)
            _invalidate_daily_values(paths)
            portfolio = expected

    _repair_trailing_shadow_rows(paths, portfolio)

    if portfolio.empty:
        new = txn
    else:
        new = txn.iloc[len(portfolio):]
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
        voo_row = _build_shadow_row(date_str, "VOO", total, paths)
        qqq_row = _build_shadow_row(date_str, "QQQ", total, paths)
        if voo_row is not None:
            voo_rows.append(voo_row)
        if qqq_row is not None:
            qqq_rows.append(qqq_row)

    _append_rows(paths["portfolio"], portfolio_rows)
    if voo_rows:
        _append_rows(paths["shadow_voo"], voo_rows)
    if qqq_rows:
        _append_rows(paths["shadow_qqq"], qqq_rows)
    _invalidate_daily_values(paths)

    return len(portfolio_rows)


def load_all(paths):
    """Return all three portfolios as DataFrames."""
    return (
        read_csv(paths["portfolio"]),
        read_csv(paths["shadow_voo"]),
        read_csv(paths["shadow_qqq"]),
    )


# Sentinel written as the sidecar's first line. Its presence means this price
# cache has provisional-row tracking, so an *empty* date set can be trusted as
# "everything is a settled close" rather than "never tracked". Without it, a
# missing file would be ambiguous and legacy caches could not be detected.
_PROVISIONAL_HEADER = "#v1 provisional-price-dates"


def _read_provisional_dates(paths):
    """Return the set of date strings whose price_history rows are intraday snapshots.

    A provisional row was written while the market was open, so it holds a
    mid-session price rather than that day's official close. It must never be
    used as a prior-close baseline, and must be re-fetched once the day closes."""
    path = paths.get("provisional_prices")
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            return {line.strip() for line in f
                    if line.strip() and not line.startswith("#")}
    except OSError:
        return set()


def _price_cache_is_tracked(paths):
    """Return True if this price cache records which rows are provisional.

    False means the cache predates provisional tracking, so nothing is known
    about whether its newest row holds a real close or an intraday snapshot."""
    path = paths.get("provisional_prices")
    return bool(path) and os.path.exists(path)


def _migrate_legacy_price_cache(paths, cached, fetched_cols):
    """Establish provisional tracking for a price cache written before it existed.

    A legacy cache's newest fetched row may be an intraday snapshot that was
    frozen in as that day's close — the original bug. Which rows are affected
    was never recorded, but only the newest one can still be wrong: any earlier
    intraday row was already overwritten by a later backfill covering its date.
    So treat just the newest row as provisional. It is then re-fetched on this
    run and excluded from baselines meanwhile, and the cache self-heals at the
    cost of one extra day of prices rather than a full history rebuild.

    Older rows are left alone deliberately: rewriting all of history would be
    slow, and the intraday-frozen row is by construction the most recent one."""
    if _price_cache_is_tracked(paths):
        return
    newest = None
    if not cached.empty and fetched_cols:
        idx = cached[fetched_cols].dropna(how="all").index
        if len(idx):
            newest = max(idx).strftime("%Y-%m-%d")
    # Write the sentinel either way, so this check runs once per cache.
    _write_provisional_dates(paths, {newest} if newest else set())


def _mark_provisional(paths, date_str):
    """Record that price_history's row for date_str is an intraday snapshot."""
    dates = _read_provisional_dates(paths)
    dates.add(str(date_str))
    _write_provisional_dates(paths, dates)


def _write_provisional_dates(paths, dates):
    """Persist the provisional-date set.

    The file is kept even when the set is empty: the sentinel line is what marks
    this cache as tracked, and removing it would make the cache look legacy and
    re-trigger migration on the next run."""
    path = paths.get("provisional_prices")
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [_PROVISIONAL_HEADER] + sorted(str(d) for d in dates)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _price_policy_is_current(paths):
    path = paths.get("price_policy") or paths["price_history"] + ".policy"
    try:
        with open(path) as policy_file:
            return policy_file.read().strip() == PRICE_POLICY
    except OSError:
        return False


def _write_price_policy(paths):
    path = paths.get("price_policy") or paths["price_history"] + ".policy"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as policy_file:
        policy_file.write(PRICE_POLICY + "\n")


def _clear_provisional(paths, date_str):
    """Drop date_str from the provisional set (its real close has been stored)."""
    dates = _read_provisional_dates(paths)
    if str(date_str) in dates:
        dates.discard(str(date_str))
        _write_provisional_dates(paths, dates)


def _settled_prices(prices_df, paths, before_date=None):
    """Return prices_df with provisional (intraday) rows removed.

    Optionally also drop rows on or after before_date. The result contains only
    official closes, so its last row is a valid prior-close baseline."""
    if prices_df.empty:
        return prices_df
    result = prices_df
    provisional = _read_provisional_dates(paths)
    if provisional:
        keep = [d.strftime("%Y-%m-%d") not in provisional for d in result.index]
        result = result[keep]
    if before_date is not None:
        result = result[result.index.date < before_date]
    return result


def _is_market_open():
    from market_calendar import is_open
    return is_open()


def _last_market_close():
    from market_calendar import last_close
    return last_close()


def _get_all_tickers(paths):
    """Return set of all tickers across all three portfolios."""
    tickers = set()
    uses_event_ledger = False
    if os.path.exists(paths["transactions"]):
        source = pd.read_csv(paths["transactions"])
        if not source.empty and "TICKER" in source.columns:
            tickers.update(source["TICKER"].dropna().astype(str))
            uses_event_ledger = "ACTION" in source.columns
    for path in [paths["portfolio"], paths["shadow_voo"], paths["shadow_qqq"]]:
        df = read_csv(path)
        if not df.empty:
            tickers.update(df["TICKER"].unique())
    if tickers and uses_event_ledger:
        tickers.update(BENCHMARK_SHADOW_PATHS)
    return tickers


def _fetch_splits(tickers, start=None, through=None):
    if start is not None and through is not None:
        return split_history.fetch_history(next(iter(tickers)), start, through)
    raise ValueError("Split requests require explicit coverage dates")


def _merge_cached_actions(path, rows, columns):
    """An incomplete provider response must not erase known corporate actions."""
    existing = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=columns)
    fetched = pd.DataFrame(rows, columns=columns)
    combined = pd.concat([existing, fetched], ignore_index=True)
    if combined.empty:
        return []
    return (combined.drop_duplicates(["TICKER", "DATE"], keep="last")
            .sort_values(["TICKER", "DATE"])[columns].values.tolist())


def sync_splits(paths, force=False):
    changed = split_history.sync(paths, _last_market_close(),
        fetch=lambda ticker, start, through: _fetch_splits([ticker], start, through), force=force)
    if changed:
        _invalidate_daily_values(paths)
    return changed


def _fetch_dividends(tickers):
    """Fetch dividend history for a set of tickers."""
    rows = []
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            divs = t.dividends
            if divs is not None and len(divs) > 0:
                for date, amount in divs.items():
                    date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
                    rows.append([ticker, date_str, round(float(amount), 6)])
        except Exception:
            continue
    return rows


def _csv_has_data_rows(path):
    if not os.path.exists(path):
        return False
    try:
        return len(pd.read_csv(path)) > 0
    except Exception:
        return False


def sync_dividends(paths):
    """Refresh dividend data if stale."""
    last_close = _last_market_close()

    if os.path.exists(paths["dividends"]):
        mtime = datetime.fromtimestamp(
            os.path.getmtime(paths["dividends"]), tz=ZoneInfo("America/New_York")
        )
        if mtime.date() >= last_close and _csv_has_data_rows(paths["dividends"]):
            return False

    tickers = _get_all_tickers(paths)
    if not tickers:
        return False

    rows = _merge_cached_actions(paths["dividends"], _fetch_dividends(tickers), ["TICKER", "DATE", "AMOUNT"])
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


def enrich_portfolio(portfolio_df, splits_df=None, dividends_df=None, current_prices=None, paths=None, manual_dividends=None):
    """Add CURRENT_SHARES, CURRENT_VALUE, and TOTAL_DIVIDENDS columns.

    manual_dividends is an optional {ticker: total_dollars} of lump-sum dividends
    received (e.g. Fundrise). Each ticker's total is distributed across its rows
    proportional to current shares and added to per-share (yfinance) dividends."""
    if portfolio_df.empty:
        return portfolio_df, 0.0, 0.0

    if splits_df is None:
        splits_df = _read_splits(paths) if paths else pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
    if dividends_df is None:
        dividends_df = _read_dividends(paths) if paths else pd.DataFrame(columns=["TICKER", "DATE", "AMOUNT"])
    if manual_dividends is None:
        manual_dividends = manual_dividends_by_ticker(paths) if paths else {}
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

    # Total adjusted shares per ticker, to allocate lump-sum dividends by weight.
    ticker_total_shares = {}
    if manual_dividends:
        for _, row in portfolio_df.iterrows():
            ticker = row["TICKER"]
            adj = get_adjusted_shares(ticker, row["SHARES_PURCHASED"], row["DATE"], splits_df)
            ticker_total_shares[ticker] = ticker_total_shares.get(ticker, 0.0) + adj

    for _, row in portfolio_df.iterrows():
        ticker = row["TICKER"]
        adj = get_adjusted_shares(ticker, row["SHARES_PURCHASED"], row["DATE"], splits_df)
        price = current_prices.get(ticker, 0.0)
        divs = get_total_dividends(ticker, row["SHARES_PURCHASED"], row["DATE"], splits_df, dividends_df)
        # Add this row's share of any lump-sum dividends received for the ticker.
        lump = manual_dividends.get(ticker, 0.0)
        if lump and ticker_total_shares.get(ticker, 0.0) > 0:
            divs += lump * (adj / ticker_total_shares[ticker])
        current_shares.append(adj)
        current_values.append(round(adj * price, 2))
        total_dividends.append(divs)

    enriched = portfolio_df.copy()
    enriched["CURRENT_SHARES"] = current_shares
    enriched["CURRENT_VALUE"] = current_values
    enriched["TOTAL_DIVIDENDS"] = total_dividends
    enriched["TOTAL_RETURN"] = [round(cv + td, 2) for cv, td in zip(current_values, total_dividends)]
    enriched["GAIN_LOSS"] = [round(cv + td - tv, 2) for cv, td, tv in zip(current_values, total_dividends, enriched["TOTAL_VALUE"])]
    return enriched, round(sum(current_values), 2), round(sum(total_dividends), 2)


def compute_irr(invested, total_return, purchase_date_str, terminal_date_str=None):
    """Annualized return for one investment using its terminal total value.

    This intentionally treats dividends as part of the terminal value rather
    than as dated interim cash flows.  ``terminal_date_str`` freezes a closed
    investment on its sale date; legacy open-position callers continue to use
    the current date.
    """
    from datetime import datetime
    try:
        terminal_date = (
            pd.to_datetime(terminal_date_str)
            if terminal_date_str is not None
            else datetime.now()
        )
        days = (terminal_date - pd.to_datetime(purchase_date_str)).days
        if days <= 0 or invested <= 0 or total_return <= 0:
            return None
        return round(((total_return / invested) ** (365.0 / days) - 1) * 100, 2)
    except Exception:
        return None


def add_comparison_columns(main_df, voo_df, qqq_df):
    """Add IRR, vs VOO, vs QQQ columns to the main portfolio dataframe."""
    irrs = []
    vs_voo = []
    vs_qqq = []
    for i, row in main_df.iterrows():
        invested = row["TOTAL_VALUE"]
        irrs.append(compute_irr(invested, row["TOTAL_RETURN"], row["DATE"]))
        if i < len(voo_df):
            vs_voo.append(round(row["TOTAL_RETURN"] - voo_df.iloc[i]["TOTAL_RETURN"], 2))
        else:
            vs_voo.append(None)
        if i < len(qqq_df):
            vs_qqq.append(round(row["TOTAL_RETURN"] - qqq_df.iloc[i]["TOTAL_RETURN"], 2))
        else:
            vs_qqq.append(None)
    main_df = main_df.copy()
    main_df["IRR"] = irrs
    main_df["VS_VOO"] = vs_voo
    main_df["VS_QQQ"] = vs_qqq
    return main_df


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
                       end=end.strftime("%Y-%m-%d"), progress=False,
                       auto_adjust=False)
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
        history_start = earliest - pd.Timedelta(days=7)
        data = yf.download(
            new_tickers, start=history_start.strftime("%Y-%m-%d"),
            progress=False, auto_adjust=True,
        )["Close"]
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
            delta = yf.download(
                existing_tickers, start=start, progress=False, auto_adjust=True
            )
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


def _vectorized_portfolio_values(portfolio_df, splits_df, dividends_df, prices_df):
    """Compute daily portfolio value series using vectorized pandas operations.
    Iterates over holdings (not days) — O(holdings) pandas ops."""
    if portfolio_df.empty or prices_df.empty:
        return pd.Series(0.0, index=prices_df.index)

    dates = prices_df.index
    totals = pd.Series(0.0, index=dates)

    for _, row in portfolio_df.iterrows():
        ticker = row["TICKER"]
        purchase_dt = pd.to_datetime(row["DATE"])
        shares = float(row["SHARES_PURCHASED"])
        if ticker not in prices_df.columns:
            continue
        adj = get_adjusted_shares(ticker, shares, row["DATE"], splits_df)
        # Vectorized: price × adjusted shares, zeroed before purchase date
        price_series = prices_df[ticker].ffill().fillna(0.0)
        mask = (dates >= purchase_dt).astype(float)
        totals += price_series * adj * mask
        # Vectorized dividends: cumulative sum applied as step function
        if not dividends_df.empty:
            ticker_divs = dividends_df[
                (dividends_df["TICKER"] == ticker) &
                (pd.to_datetime(dividends_df["DATE"]) > purchase_dt)
            ]
            for _, div in ticker_divs.iterrows():
                div_dt = pd.to_datetime(div["DATE"])
                div_amount = adj * float(div["AMOUNT"])
                totals += div_amount * (dates >= div_dt).astype(float)

    return totals


def compute_daily_values(paths):
    """Compute or update cached daily portfolio values for the chart.
    Uses vectorized computation. Caches to daily_values.csv."""
    source = pd.read_csv(paths["transactions"]) if os.path.exists(paths["transactions"]) else pd.DataFrame()
    if not source.empty and "ACTION" in source.columns:
        history = portfolio_replay.build_history(paths)
        if history:
            directory = os.path.dirname(paths["daily_values"])
            os.makedirs(directory, exist_ok=True)
            data_fd, data_temp = tempfile.mkstemp(
                prefix="daily_values.", suffix=".tmp", dir=directory
            )
            meta_fd, meta_temp = tempfile.mkstemp(
                prefix="daily_values_meta.", suffix=".tmp", dir=directory
            )
            try:
                os.close(data_fd)
                pd.DataFrame(history).to_csv(data_temp, index=False)
                with os.fdopen(meta_fd, "w") as meta_file:
                    json.dump({
                        "mode": "accounting-v1",
                        "input_sha256": _accounting_input_fingerprint(paths),
                    }, meta_file, sort_keys=True)
                os.replace(data_temp, paths["daily_values"])
                os.replace(meta_temp, paths["daily_values"] + ".meta")
            except Exception:
                try:
                    os.close(meta_fd)
                except OSError:
                    pass
                for temp_path in (data_temp, meta_temp):
                    try:
                        os.unlink(temp_path)
                    except FileNotFoundError:
                        pass
                raise
        return history
    port_df = read_csv(paths["portfolio"])
    voo_df = read_csv(paths["shadow_voo"])
    qqq_df = read_csv(paths["shadow_qqq"])
    if port_df.empty:
        return []

    prices_path = paths["price_history"]
    if not os.path.exists(prices_path):
        return []
    prices_df = pd.read_csv(prices_path, index_col=0, parse_dates=True)
    if prices_df.empty:
        return []

    splits_df = _read_splits(paths)
    dividends_df = _read_dividends(paths)

    earliest = pd.to_datetime(port_df["DATE"]).min()
    prices_df = prices_df.loc[earliest:]
    if prices_df.empty:
        return []

    # Forward-fill so holdings priced only on transaction dates (e.g. manual
    # portfolios whose latest price predates today) carry their last known price
    # forward. Without this the delta-cache slice can start on an all-NaN row and
    # collapse the portfolio value to 0 on recent days.
    prices_df = prices_df.ffill()

    # Check cache validity
    cache_path = paths["daily_values"]
    txn_count = len(port_df) + len(voo_df) + len(qqq_df)
    meta_path = cache_path + ".meta"

    if os.path.exists(cache_path) and os.path.exists(meta_path):
        with open(meta_path) as f:
            cached_count = int(f.read().strip())
        if cached_count == txn_count:
            cached = pd.read_csv(cache_path, parse_dates=["DATE"])
            last_cached = cached["DATE"].max()
            new_prices = prices_df.loc[last_cached:]
            if new_prices.empty:
                return cached.to_dict("records")
            # Delta: recompute from the last cached date so intraday rows for
            # an existing date replace stale cached values.
            main_vals = _vectorized_portfolio_values(port_df, splits_df, dividends_df, new_prices)
            voo_vals = _vectorized_portfolio_values(voo_df, splits_df, dividends_df, new_prices)
            qqq_vals = _vectorized_portfolio_values(qqq_df, splits_df, dividends_df, new_prices)
            new_df = pd.DataFrame({
                "DATE": new_prices.index.strftime("%Y-%m-%d"),
                "MAIN": main_vals.round(2).values,
                "VOO": voo_vals.round(2).values,
                "QQQ": qqq_vals.round(2).values,
            })
            cached = cached[cached["DATE"] < last_cached]
            combined = pd.concat([cached, new_df], ignore_index=True)
            combined.to_csv(cache_path, index=False)
            return combined.to_dict("records")

    # Full compute
    main_vals = _vectorized_portfolio_values(port_df, splits_df, dividends_df, prices_df)
    voo_vals = _vectorized_portfolio_values(voo_df, splits_df, dividends_df, prices_df)
    qqq_vals = _vectorized_portfolio_values(qqq_df, splits_df, dividends_df, prices_df)

    result = pd.DataFrame({
        "DATE": prices_df.index.strftime("%Y-%m-%d"),
        "MAIN": main_vals.round(2).values,
        "VOO": voo_vals.round(2).values,
        "QQQ": qqq_vals.round(2).values,
    })
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    result.to_csv(cache_path, index=False)
    with open(meta_path, "w") as f:
        f.write(str(txn_count))
    return result.to_dict("records")


def get_cached_daily_values(paths):
    """Read cached daily values for chart rendering. No computation."""
    cache_path = paths["daily_values"]
    if os.path.exists(cache_path):
        df = pd.read_csv(cache_path)
        if not df.empty:
            return df.astype(object).where(pd.notna(df), None).to_dict("records")
    return []


def get_last_updated(paths):
    """Read the last_updated file and return a human-readable display string."""
    if os.path.exists(paths["last_updated"]):
        with open(paths["last_updated"]) as f:
            raw = f.read().strip()
        try:
            if "|intraday" in raw:
                ts = raw.split("|")[0]
                dt = pd.to_datetime(ts)
                return "Prices as of " + dt.strftime("%-I:%M %p ET") + " (market open)"
            elif "|close" in raw:
                ds = raw.split("|")[0]
                dt = pd.to_datetime(ds)
                return "Prices as of market close " + dt.strftime("%B %-d, %Y")
            else:
                dt = pd.to_datetime(raw)
                return "Prices as of market close " + dt.strftime("%B %-d, %Y")
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
        date_part = raw.split("|")[0]
        updated_date = pd.to_datetime(date_part).date()
        return updated_date < _last_market_close()
    except Exception:
        return True


def should_auto_refresh(paths):
    """Return True when the page should automatically refresh cached data."""
    if _is_market_open():
        return True
    return needs_refresh(paths)


def _set_last_updated(paths, close_date=None, intraday=False):
    """Write last-updated info. Stores ISO timestamp for intraday, date for closing."""
    os.makedirs(os.path.dirname(paths["last_updated"]), exist_ok=True)
    et = ZoneInfo("America/New_York")
    if intraday:
        val = datetime.now(et).strftime("%Y-%m-%dT%H:%M") + " ET|intraday"
    else:
        val = str(close_date) + "|close"
    with open(paths["last_updated"], "w") as f:
        f.write(val)
    return val


def update_prices(paths, max_retries=3, allow_partial_history=False):
    """Compatibility entry point for the isolated API acquisition adapter."""
    import price_provider
    return price_provider.refresh(paths, _last_market_close(), _is_market_open(),
                                  max_retries, allow_partial_history)


def _baseline_date(prices_df, paths, market_open):
    """Return the date whose closes are the correct 'previous close' baseline.

    This is the latest date in price_history that (a) holds an official close
    rather than an intraday snapshot, and (b) precedes the session currently
    being measured. Returns None when no such date exists.

    Using the latest *settled* close — rather than positional indexing like
    iloc[-2] — is what keeps today's change correct. A provisional row holds a
    mid-session price, and comparing against it understates or overstates the
    day's move by however far that snapshot sat from the real close."""
    if prices_df.empty:
        return None
    settled = _settled_prices(prices_df, paths)
    if settled.empty:
        return None
    if market_open:
        # Measuring today's in-progress session: baseline is the last close
        # strictly before today.
        today = datetime.now(ZoneInfo("America/New_York")).date()
        prior = settled[settled.index.date < today]
    else:
        # Measuring the most recent completed session: baseline is the last
        # settled close strictly before that session's date.
        latest = prices_df.index.max().date()
        prior = settled[settled.index.date < latest]
    if prior.empty:
        return None
    return prior.index.max()


def get_market_comparison(portfolio_total, voo_total, qqq_total, paths):
    """Compare current portfolio values to prior close for the 'Vs. the Market Today' card.
    Returns dict with changes and deltas, or None if insufficient data.

    The baseline is always the most recent *official close* preceding the session
    being measured. Intraday snapshots are excluded, so a mid-session price can
    never masquerade as a previous close."""
    if any(value is None for value in (portfolio_total, voo_total, qqq_total)):
        return None
    if _manual_tickers(paths):
        return None  # No market-session change for a mixed/static valuation total.
    cache_path = paths["daily_values"]
    if not os.path.exists(cache_path):
        return None
    df = pd.read_csv(cache_path)
    if len(df) < 2:
        return None

    market_open = _is_market_open()

    def _daily_values_base():
        """Fallback baseline from the cached daily-value series."""
        last_date = pd.to_datetime(df.iloc[-1]["DATE"]).date()
        today = datetime.now(ZoneInfo("America/New_York")).date()
        return df.iloc[-2] if last_date == today else df.iloc[-1]

    price_history_path = paths["price_history"]
    prices_df = pd.DataFrame()
    if os.path.exists(price_history_path):
        prices_df = pd.read_csv(price_history_path, index_col=0, parse_dates=True)

    base_date = _baseline_date(prices_df, paths, market_open) if not prices_df.empty else None

    source = (
        pd.read_csv(paths["transactions"])
        if os.path.exists(paths["transactions"]) else pd.DataFrame()
    )
    uses_event_ledger = not source.empty and "ACTION" in source.columns

    if uses_event_ledger:
        book = pricing_sources.load_book(paths)
        snapshot = build_accounting_snapshot(paths)
        session = datetime.now(ZoneInfo("America/New_York")).date() if market_open else _last_market_close()
        tickers = {h["ticker"] for h in snapshot["actual"]["holdings"]} | {"VOO", "QQQ"}
        if base_date is None:
            return None
        for ticker in tickers:
            current = book.resolve(ticker, session)
            baseline = book.resolve(ticker, base_date.date(), "session_close")
            if (current.effective_date != session or current.kind not in {"close", "intraday"}
                    or baseline.value is None):
                return None

    if uses_event_ledger and base_date is not None:
        # Event-ledger history already includes sales, actual cash, and the
        # continuous benchmarks. Legacy generated holding files cannot provide
        # a correct baseline once a lot has been sold.
        dated = df.copy()
        dated["DATE"] = pd.to_datetime(dated["DATE"])
        prior = dated[dated["DATE"] <= base_date]
        if prior.empty:
            return None
        base = prior.iloc[-1]
        port_base, voo_base, qqq_base = base["MAIN"], base["VOO"], base["QQQ"]
        port_change = portfolio_total - port_base
        voo_change = voo_total - voo_base
        qqq_change = qqq_total - qqq_base
    elif base_date is not None:
        # Value every holding at the baseline date's closes. Slicing through
        # base_date (rather than passing a single row) lets the existing
        # forward-fill carry prices for holdings that did not trade that day.
        base_prices = prices_df.loc[:base_date]
        splits_df = _read_splits(paths)
        dividends_df = _read_dividends(paths)
        port_df, voo_df, qqq_df = load_all(paths)
        port_base = _vectorized_portfolio_values(port_df, splits_df, dividends_df, base_prices).iloc[-1]
        voo_base = _vectorized_portfolio_values(voo_df, splits_df, dividends_df, base_prices).iloc[-1]
        qqq_base = _vectorized_portfolio_values(qqq_df, splits_df, dividends_df, base_prices).iloc[-1]
        # Compare current (live or latest) totals against the settled baseline.
        port_change = portfolio_total - port_base
        voo_change = voo_total - voo_base
        qqq_change = qqq_total - qqq_base
    elif market_open:
        # No price history but market is open: compare live totals against the
        # last cached daily value that is not today's in-progress row.
        prev = _daily_values_base()
        port_base, voo_base, qqq_base = prev["MAIN"], prev["VOO"], prev["QQQ"]
        port_change = portfolio_total - port_base
        voo_change = voo_total - voo_base
        qqq_change = qqq_total - qqq_base
    else:
        # Market closed, no price history: last cached close vs the prior one.
        last = df.iloc[-1]
        prev = df.iloc[-2]
        port_base, voo_base, qqq_base = prev["MAIN"], prev["VOO"], prev["QQQ"]
        port_change = last["MAIN"] - port_base
        voo_change = last["VOO"] - voo_base
        qqq_change = last["QQQ"] - qqq_base

    if any(pd.isna(value) for value in (port_base, voo_base, qqq_base, port_change, voo_change, qqq_change)):
        return None

    def _pct(change, base):
        return round(change / base * 100, 2) if base else 0.0

    return {
        "portfolio_change": round(port_change, 2),
        "portfolio_pct": _pct(port_change, port_base),
        "voo_change": round(voo_change, 2),
        "voo_pct": _pct(voo_change, voo_base),
        "qqq_change": round(qqq_change, 2),
        "qqq_pct": _pct(qqq_change, qqq_base),
        "vs_voo": round(port_change - voo_change, 2),
        "vs_qqq": round(port_change - qqq_change, 2),
        "market_open": market_open,
    }


def get_gainers_losers(portfolio_summary_data, paths, n=5):
    """Return top/bottom n by dollar change and by percent change.
    Returns (dollar_gainers, dollar_losers, pct_gainers, pct_losers).

    Uses the same settled prior-close baseline as get_market_comparison, so the
    per-ticker percentages agree with the portfolio's headline change."""
    prices_path = paths["price_history"]
    if not os.path.exists(prices_path) or not portfolio_summary_data:
        return [], [], [], []
    prices_df = pd.read_csv(prices_path, index_col=0, parse_dates=True)
    if len(prices_df) < 2:
        return [], [], [], []

    base_date = _baseline_date(prices_df, paths, _is_market_open())
    if base_date is None:
        return [], [], [], []
    # Latest prices available (may be an intraday snapshot — that is correct for
    # the current side of the comparison), against the last settled close.
    session_date = prices_df.index.max()
    if base_date >= session_date:
        return [], [], [], []  # No newer session quote: do not manufacture zero movers.
    # A stale carried-forward quote is useful for valuation, but cannot prove
    # a daily price change. Only rank symbols quoted on both comparison dates.
    today_prices = prices_df.loc[session_date]
    prev_prices = prices_df.loc[base_date]
    splits = _read_splits(paths) if _price_policy_is_current(paths) else pd.DataFrame()

    book = pricing_sources.load_book(paths)
    event_ledger = "ACTION" in pricing_sources.read_frame(paths.get("transactions")).columns
    changes = []
    for row in portfolio_summary_data:
        ticker = row["TICKER"]
        shares = row["SHARES_OWNED"]
        if ticker in today_prices.index and ticker in prev_prices.index:
            tp = today_prices[ticker]
            pp = prev_prices[ticker]
            if event_ledger:
                current = book.resolve(ticker, session_date.date())
                prior = book.resolve(ticker, base_date.date(), "session_close")
                if (current.effective_date != session_date.date() or current.kind not in {"close", "intraday"}
                        or prior.value is None or book.modes.get(ticker) == "static"):
                    continue
                tp, pp = float(current.value), float(prior.value)
            if pd.notna(tp) and pd.notna(pp) and pp != 0:
                if not splits.empty:
                    split_dates = pd.to_datetime(splits["DATE"])
                    relevant = splits[(splits["TICKER"] == ticker)
                                      & (split_dates > base_date)
                                      & (split_dates <= session_date)]
                    if not relevant.empty:
                        pp /= pd.to_numeric(relevant["RATIO"]).prod()
                dollar = round((tp - pp) * shares, 2)
                pct = round((tp - pp) / pp * 100, 2)
                changes.append({"TICKER": ticker, "CHANGE": dollar, "PCT": pct})
    by_dollar = sorted(changes, key=lambda x: x["CHANGE"], reverse=True)
    by_pct = sorted(changes, key=lambda x: x["PCT"], reverse=True)
    return by_dollar[:n], by_dollar[-n:][::-1], by_pct[:n], by_pct[-n:][::-1]


def _empty_baseball_stats(benchmark, integrity_ok=True, message=None):
    return {
        "benchmark": benchmark,
        "batting_average": None,
        "slugging_percentage": None,
        "slugging_buckets": [],
        "daily_win_percentage": None,
        "counts": {
            "transaction_wins": 0,
            "transaction_losses": 0,
            "transaction_ties": 0,
            "slugging_bases": 0,
            "slugging_transactions": 0,
            "daily_wins": 0,
            "daily_losses": 0,
            "daily_ties": 0,
        },
        "integrity": {
            "ok": integrity_ok,
            "message": message,
        },
    }


def _cached_current_prices(paths, *portfolios):
    if not os.path.exists(paths["price_history"]):
        return {}
    prices_df = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
    if prices_df.empty:
        return {}
    tickers = set()
    for portfolio_df in portfolios:
        if not portfolio_df.empty and "TICKER" in portfolio_df.columns:
            tickers.update(portfolio_df["TICKER"].dropna().astype(str))
    current_prices = {}
    for ticker in tickers:
        if ticker in prices_df.columns:
            last_idx = prices_df[ticker].last_valid_index()
            if last_idx is not None:
                current_prices[ticker] = round(float(prices_df[ticker].loc[last_idx]), 2)
    return current_prices


def _baseball_batting_and_slugging(paths, benchmark, main_df, benchmark_df):
    stats = _empty_baseball_stats(benchmark)
    if main_df.empty:
        return stats
    if len(main_df) != len(benchmark_df):
        stats["integrity"] = {
            "ok": False,
            "message": (
                f"{benchmark} shadow row count ({len(benchmark_df)}) does not match "
                f"portfolio row count ({len(main_df)})."
            ),
        }
        return stats

    splits_df = _read_splits(paths)
    dividends_df = _read_dividends(paths)
    current_prices = _cached_current_prices(paths, main_df, benchmark_df)
    main_enriched, _, _ = enrich_portfolio(main_df, splits_df, dividends_df, current_prices)
    benchmark_enriched, _, _ = enrich_portfolio(benchmark_df, splits_df, dividends_df, current_prices)

    wins = losses = ties = 0
    bases = 0
    bucket_map = {}
    for idx in range(len(main_enriched)):
        main_return = float(main_enriched.iloc[idx]["TOTAL_RETURN"]) - float(main_enriched.iloc[idx]["TOTAL_VALUE"])
        benchmark_return = float(benchmark_enriched.iloc[idx]["TOTAL_RETURN"]) - float(benchmark_enriched.iloc[idx]["TOTAL_VALUE"])
        if main_return > benchmark_return:
            wins += 1
        elif main_return < benchmark_return:
            losses += 1
        else:
            ties += 1

        invested = float(main_enriched.iloc[idx]["TOTAL_VALUE"])
        if invested > 0:
            multiple = float(main_enriched.iloc[idx]["TOTAL_RETURN"]) / invested
            row_bases = max(0, int(multiple) - 1)
            bases += row_bases
            if row_bases > 0:
                bucket = bucket_map.setdefault(row_bases, {"bases": row_bases, "count": 0, "tickers": []})
                bucket["count"] += 1
                ticker = str(main_enriched.iloc[idx]["TICKER"])
                if ticker not in bucket["tickers"]:
                    bucket["tickers"].append(ticker)

    denominator = wins + losses
    stats["batting_average"] = round(wins / denominator, 4) if denominator else None
    stats["slugging_percentage"] = round(bases / len(main_enriched), 4) if len(main_enriched) else None
    stats["slugging_buckets"] = [bucket_map[key] for key in sorted(bucket_map)]
    stats["counts"].update({
        "transaction_wins": wins,
        "transaction_losses": losses,
        "transaction_ties": ties,
        "slugging_bases": bases,
        "slugging_transactions": len(main_enriched),
    })
    return stats


def _baseball_daily_win_percentage(paths, benchmark, main_df, benchmark_df):
    if main_df.empty or benchmark_df.empty or not os.path.exists(paths["price_history"]):
        return None, {"daily_wins": 0, "daily_losses": 0, "daily_ties": 0}

    prices_df = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
    if prices_df.empty:
        return None, {"daily_wins": 0, "daily_losses": 0, "daily_ties": 0}

    earliest = pd.to_datetime(main_df["DATE"]).min()
    prices_df = prices_df.loc[earliest:]
    if prices_df.empty:
        return None, {"daily_wins": 0, "daily_losses": 0, "daily_ties": 0}

    splits_df = _read_splits(paths)
    empty_dividends = pd.DataFrame(columns=["TICKER", "DATE", "AMOUNT"])
    main_values = _vectorized_portfolio_values(main_df, splits_df, empty_dividends, prices_df)
    benchmark_values = _vectorized_portfolio_values(benchmark_df, splits_df, empty_dividends, prices_df)

    main_pct = main_values.pct_change()
    benchmark_pct = benchmark_values.pct_change()
    valid = main_pct.notna() & benchmark_pct.notna()
    valid &= main_values.shift(1).fillna(0) > 0
    valid &= benchmark_values.shift(1).fillna(0) > 0

    wins = int((main_pct[valid] > benchmark_pct[valid]).sum())
    losses = int((main_pct[valid] < benchmark_pct[valid]).sum())
    ties = int((main_pct[valid] == benchmark_pct[valid]).sum())
    denominator = wins + losses
    value = round(wins / denominator, 4) if denominator else None
    return value, {"daily_wins": wins, "daily_losses": losses, "daily_ties": ties}


def get_baseball_stats(paths, benchmark="VOO"):
    """Return read-only baseball-style stats for a portfolio against a benchmark."""
    if benchmark not in BENCHMARK_SHADOW_PATHS:
        return _empty_baseball_stats(benchmark, integrity_ok=False, message=f"Unsupported benchmark: {benchmark}")

    source = pd.read_csv(paths["transactions"]) if os.path.exists(paths["transactions"]) else pd.DataFrame()
    if not source.empty and "ACTION" in source.columns:
        snapshot = build_accounting_snapshot(paths)
        stats = _empty_baseball_stats(benchmark)
        stats["pricing_note"] = (
            "Benchmark comparisons are unavailable; investment multiples remain available."
            if snapshot["portfolio_benchmarks"][benchmark]["total_value"] is None else
            "Returns use dated valuations. Carried or static prices can smooth daily returns."
        )
        wins = losses = ties = bases = 0
        bucket_map = {}
        for decision in snapshot["decisions"]:
            actual_value = float(decision["actual"]["total_return_value"])
            invested = float(decision["original_investment"])
            if invested > 0:
                row_bases = max(0, int(actual_value / invested) - 1)
                bases += row_bases
                if row_bases:
                    bucket = bucket_map.setdefault(
                        row_bases, {"bases": row_bases, "count": 0, "tickers": []}
                    )
                    bucket["count"] += 1
                    if decision["ticker"] not in bucket["tickers"]:
                        bucket["tickers"].append(decision["ticker"])
            if decision[benchmark]["total_return_value"] is None:
                continue
            benchmark_value = float(decision[benchmark]["total_return_value"])
            if actual_value > benchmark_value:
                wins += 1
            elif actual_value < benchmark_value:
                losses += 1
            else:
                ties += 1
        denominator = wins + losses
        stats["batting_average"] = round(wins / denominator, 4) if denominator else None
        stats["slugging_percentage"] = round(bases / len(snapshot["decisions"]), 4) if snapshot["decisions"] else None
        stats["slugging_buckets"] = [bucket_map[key] for key in sorted(bucket_map)]
        stats["counts"].update({
            "transaction_wins": wins,
            "transaction_losses": losses,
            "transaction_ties": ties,
            "slugging_bases": bases,
            "slugging_transactions": len(snapshot["decisions"]),
        })
        history = get_cached_daily_values(paths)
        if history:
            daily = pd.DataFrame(history)
            benchmark_column = benchmark
            if {"MAIN", benchmark_column}.issubset(daily.columns):
                main_pct = pd.to_numeric(daily["MAIN"], errors="coerce").pct_change(fill_method=None)
                benchmark_pct = pd.to_numeric(daily[benchmark_column], errors="coerce").pct_change(fill_method=None)
                valid = main_pct.notna() & benchmark_pct.notna()
                daily_wins = int((main_pct[valid] > benchmark_pct[valid]).sum())
                daily_losses = int((main_pct[valid] < benchmark_pct[valid]).sum())
                daily_ties = int((main_pct[valid] == benchmark_pct[valid]).sum())
                daily_denominator = daily_wins + daily_losses
                stats["daily_win_percentage"] = (
                    round(daily_wins / daily_denominator, 4) if daily_denominator else None
                )
                stats["counts"].update({
                    "daily_wins": daily_wins,
                    "daily_losses": daily_losses,
                    "daily_ties": daily_ties,
                })
        return stats

    main_df = _read_existing_csv(paths["portfolio"], COLUMNS)
    benchmark_key = BENCHMARK_SHADOW_PATHS[benchmark]
    benchmark_df = _read_existing_csv(paths[benchmark_key], COLUMNS)

    stats = _baseball_batting_and_slugging(paths, benchmark, main_df, benchmark_df)
    if not stats["integrity"]["ok"]:
        return stats

    daily_value, daily_counts = _baseball_daily_win_percentage(paths, benchmark, main_df, benchmark_df)
    stats["daily_win_percentage"] = daily_value
    stats["counts"].update(daily_counts)
    return stats


def build_accounting_snapshot(paths, valuation_date=None, persist=False):
    """Build the sell/cash/two-benchmark accounting snapshot.

    This is the public boundary for the new replay engine.  Legacy helpers stay
    available during migration, but new UI and reporting code must use this
    snapshot rather than aggregating legacy shadow rows.
    """
    return portfolio_replay.build_snapshot(
        paths, valuation_date=valuation_date, persist=persist
    )


def get_cached_accounting_snapshot(paths):
    """Return the last complete accounting snapshot, or None if unavailable."""
    snapshot_path = paths.get("accounting_snapshot") or os.path.join(
        paths["data_dir"], "accounting_snapshot.json"
    )
    if not os.path.exists(snapshot_path):
        return None
    try:
        with open(snapshot_path) as snapshot_file:
            return json.load(snapshot_file)
    except (OSError, ValueError):
        return None


def refresh_data(paths, force=False):
    with pricing_sources.data_lock(paths, exclusive=True):
        return _refresh_data_locked(paths, force)


def _refresh_data_locked(paths, force=False):
    """Stage a refresh, retaining all last-known-good data on provider failure."""
    data_dir = paths["data_dir"]
    source_version = pricing_sources.source_fingerprint(paths)
    os.makedirs(data_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portfolio-refresh-") as staging:
        shutil.copytree(data_dir, staging, dirs_exist_ok=True)
        staged = {
            key: (os.path.join(staging, os.path.relpath(value, data_dir))
                  if os.path.commonpath([os.path.abspath(value), os.path.abspath(data_dir)])
                  == os.path.abspath(data_dir) else value)
            for key, value in paths.items()
        }
        try:
            if force:
                for key in ("daily_values", "last_updated", "accounting_snapshot"):
                    if staged.get(key) and os.path.exists(staged[key]):
                        os.unlink(staged[key])
            result = _refresh_data_staged(staged)
        except Exception as exc:
            result = {"status": "error", "message": str(exc)}
        if pricing_sources.source_fingerprint(paths) != source_version:
            return {"status": "error", "message": "Source data changed during refresh. Refresh again to use the latest ledger."}
        if result.get("status") == "ok" or result.get("usable"):
            for name in os.listdir(staging):
                source = os.path.join(staging, name)
                if os.path.isfile(source) and name != ".pricing.lock":
                    # Copy to the destination filesystem before atomic replace.
                    fd, target = tempfile.mkstemp(dir=data_dir)
                    os.close(fd)
                    try:
                        shutil.copyfile(source, target)
                        os.replace(target, os.path.join(data_dir, name))
                    finally:
                        if os.path.exists(target):
                            os.unlink(target)
        else:
            # Source observations survive a derived-calculation failure.
            for key in ("price_history", "splits", "dividends", "price_policy", "provisional_prices", "price_kinds", "price_receipts", "split_status", "split_attempts"):
                if result.get("observations_committed") and staged.get(key) and os.path.isfile(staged[key]):
                    fd, target = tempfile.mkstemp(dir=data_dir)
                    os.close(fd)
                    shutil.copyfile(staged[key], target)
                    os.replace(target, paths[key])
            if result.get("observations_committed") and os.path.exists(os.path.join(staging, "prices.sqlite3")):
                shutil.copyfile(os.path.join(staging, "prices.sqlite3"), os.path.join(data_dir, "prices.sqlite3"))
            result = dict(result)
            result["using_cached_data"] = os.path.exists(paths["price_history"])
            result["last_updated"] = get_last_updated(paths)
            result.setdefault("message", "Prices unavailable for: " + ", ".join(result.get("failed_tickers", [])))
            fallback = ("Retained previous data" if result["using_cached_data"]
                        else "No cached prices available")
            result["message"] += f". {fallback}; refresh did not advance freshness."
        with open(os.path.join(data_dir, "refresh_status.json"), "w") as status_file:
            json.dump(result, status_file)
        return result


def get_snapshot_price_label(snapshot):
    """Describe the dates of displayed quotes, not a failed refresh's old stamp."""
    dates = {lot["price_date"] for lot in snapshot["actual"]["lots"]
             if lot["status"] == "OPEN" and lot.get("price_date")}
    dates.update(value["price_date"] for value in snapshot["portfolio_benchmarks"].values()
                 if value.get("price_date"))
    if not dates:
        return "No cached quotes"
    first, last = min(dates), max(dates)
    return "Price dates: " + (first if first == last else f"{first}–{last}")


def get_refresh_warning(paths):
    try:
        with open(os.path.join(paths["data_dir"], "refresh_status.json")) as status_file:
            result = json.load(status_file)
        return result.get("message", "") if result.get("status") != "ok" else ""
    except (OSError, ValueError):
        return ""


def _refresh_data_staged(paths):
    """Full refresh: sync new transactions, splits, dividends, update prices, and recompute chart."""
    sync(paths)
    split_error = None
    try:
        sync_splits(paths)
    except Exception as exc:
        split_error = str(exc)
    try:
        sync_dividends(paths)
    except Exception:
        pass
    pricing_sources.persist_generation(paths, pricing_sources.load_book(paths), None)
    try:
        result = update_prices(paths, allow_partial_history=True)
    except Exception as exc:
        result = {"status": "incomplete", "message": str(exc), "failed_tickers": sorted(_get_all_tickers(paths) - _manual_tickers(paths))}
    pricing_sources.persist_generation(paths, pricing_sources.load_book(paths), result)
    result["observations_committed"] = True
    source = pd.read_csv(paths["transactions"]) if os.path.exists(paths["transactions"]) else pd.DataFrame()
    try:
        sync_manual_prices(paths)
    except Exception:
        pass
    try:
        history = compute_daily_values(paths)
        result["chart_points"] = len(history)
    except Exception as e:
        result = dict(result)
        result["status"] = "error"
        result["message"] = f"Prices refreshed but chart update failed: {e}"
    source = (
        pd.read_csv(paths["transactions"])
        if os.path.exists(paths["transactions"]) else pd.DataFrame()
    )
    if result.get("status") in {"ok", "incomplete"} and "ACTION" in source.columns:
        try:
            build_accounting_snapshot(paths, persist=True)
        except Exception as e:
            result = dict(result)
            result["status"] = "error"
            result["message"] = f"Prices refreshed but accounting replay failed: {e}"
    if result.get("status") == "incomplete" and "ACTION" in source.columns:
        result["usable"] = True
        count = len(result.get("failed_tickers", []))
        result["message"] = (
            ("Some prices could not be refreshed" + (f" ({count} symbols)" if count else "") + ". Using available cached prices.")
            + (" Missing historical chart values are left blank." if any(row.get("MAIN") is None for row in history) else "")
        )
    split_check = split_history.summary(paths, _last_market_close())
    result['split_history'] = split_check
    if split_error or split_check['pending']:
        result['split_history']['error'] = split_error
        if result.get('status') == 'ok':
            result['status'] = 'incomplete'
            result['usable'] = True
        result['message'] = (result.get('message', '') + ' ' + (('Split-history check failed: ' + split_error + '. ') if split_error else '') + split_check['message']).strip()
    return result


def get_price_updates(paths):
    """Price provenance used by the shared resolver, without fetching anything."""
    book = pricing_sources.load_book(paths)
    if book.latest_date is None:
        return []
    return [{"ticker": ticker, **book.resolve(ticker, book.latest_date).to_dict()}
            for ticker in sorted(set(book.observations) | _get_all_tickers(paths))]
