"""API price acquisition and cache migration, separate from portfolio valuation."""
import os
import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf
import pricing_sources

PRICE_POLICY = "unadjusted-close-v1"
_PROVISIONAL_HEADER = "#v1 provisional-price-dates"
BENCHMARK_SHADOW_PATHS = {"VOO": "shadow_voo", "QQQ": "shadow_qqq"}
read_csv = pricing_sources.read_frame


def _manual_tickers(paths):
    return {t for t, mode in pricing_sources.instrument_modes(paths).items() if mode == "static"}


def _get_all_tickers(paths):
    tickers = set(pricing_sources.instrument_modes(paths))
    for key in ("portfolio", "shadow_voo", "shadow_qqq"):
        frame = read_csv(paths.get(key))
        tickers.update(frame.get("TICKER", pd.Series(dtype=str)).dropna().astype(str))
    # Empty ledgers should not fetch benchmarks by themselves.
    tx = read_csv(paths.get("transactions"))
    if tx.empty and not any(not read_csv(paths.get(k)).empty for k in ("portfolio", "shadow_voo", "shadow_qqq")):
        return set()
    return tickers


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
    """Mark the newest untracked row for reconciliation.

    Older ambiguous history requires a separate audit; this migration cannot
    prove that earlier snapshots were final closes.
    """
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

def _crypto(ticker):
    # Yahoo crypto pairs in our portfolios use the -USD symbol convention.
    return ticker.endswith("-USD")


def _session_today(ticker):
    return datetime.now(timezone.utc if _crypto(ticker) else ZoneInfo("America/New_York")).date()


def _unfinished(ticker, day, market_open):
    today = _session_today(ticker)
    return day >= today if _crypto(ticker) else day > today or (day == today and market_open)


def _quote_kinds(paths):
    path = paths.get("price_kinds") or os.path.join(paths["data_dir"], "price_kinds.json")
    with open(path) as stream:
        return json.load(stream)


def _sync_provisional_dates(paths):
    # Compatibility summary only: finality decisions use per-security kinds.
    _write_provisional_dates(paths, {
        key.rsplit("|", 1)[1] for key, kind in _quote_kinds(paths).items()
        if kind == "intraday"
    })


def refresh(paths, close_date, market_open, max_retries=3, allow_partial_history=False):
    """Acquire observations; unavailable history never determines portfolio availability."""
    import time

    # Manually-priced tickers are never fetched from yfinance; their values come
    # from sync_manual_prices. Only benchmark/normal tickers are fetched here.
    manual_tickers = _manual_tickers(paths)
    all_tickers = sorted(t for t in _get_all_tickers(paths) if t not in manual_tickers)
    if not all_tickers:
        return {"status": "ok", "last_updated": _set_last_updated(paths, close_date=close_date)}

    source_df = (
        pd.read_csv(paths["transactions"])
        if os.path.exists(paths["transactions"]) else pd.DataFrame()
    )
    uses_event_ledger = not source_df.empty and "ACTION" in source_df.columns
    if not source_df.empty and "DATE" in source_df.columns:
        earliest = pd.to_datetime(source_df["DATE"]).min()
    else:
        port_df = read_csv(paths["portfolio"])
        earliest = pd.to_datetime(port_df["DATE"]).min() if not port_df.empty else None
    event_price_dates = {}
    event_current_tickers = set(all_tickers)
    if uses_event_ledger:
        normalized = [{"action": str(row.ACTION).upper(), "ticker": str(row.TICKER),
                       "date": pd.Timestamp(row.DATE), "transaction_id": str(row.TRANSACTION_ID),
                       "lot_id": str(row.LOT_ID)} for _, row in source_df.iterrows()]
        normalized.sort(key=lambda e: (e["date"], e["action"] != "SELL"))
        open_lots = {}
        for event in normalized:
            if event["action"] == "BUY":
                open_lots[event["transaction_id"]] = event["ticker"]
                if event["ticker"] in all_tickers:
                    event_price_dates.setdefault(event["ticker"], pd.Timestamp(event["date"]))
            else:
                open_lots.pop(event["lot_id"], None)
        event_current_tickers = (
            set(open_lots.values()) & set(all_tickers)
        ) | set(BENCHMARK_SHADOW_PATHS)
        for benchmark in BENCHMARK_SHADOW_PATHS:
            event_price_dates[benchmark] = earliest

    # Load existing cache; freshness is judged on fetched tickers only, since
    # manual tickers may carry dates the market never traded.
    if os.path.exists(paths["price_history"]):
        raw = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
        cached = raw[~raw.index.duplicated(keep="last")]
        if len(cached) < len(raw):
            cached.to_csv(paths["price_history"])
    else:
        cached = pd.DataFrame()

    price_policy_rebuild = uses_event_ledger and not _price_policy_is_current(paths)
    if price_policy_rebuild:
        # Keep the original cache as migration evidence; never destroy unknown-basis data.
        if os.path.exists(paths["price_history"]):
            backup = paths["price_history"] + ".pre-shared-pricing"
            if not os.path.exists(backup):
                shutil.copyfile(paths["price_history"], backup)
        # Old caches used yfinance's adjusted Close while this application also
        # posted dividends and splits explicitly. Preserve authoritative manual
        # price columns, but rebuild every provider-priced column from raw Close.
        keep = [ticker for ticker in manual_tickers if ticker in cached.columns]
        cached = cached[keep].copy() if keep else pd.DataFrame()

    # Provisional rows hold intraday snapshots, not official closes. A row for
    # close_date that is still provisional means the market has since closed
    # without us re-fetching, so the cache is NOT current — otherwise the stale
    # mid-session price would be frozen into history as that day's close.
    fetched_cols = [t for t in all_tickers if not cached.empty and t in cached.columns]
    # A cache written before provisional tracking existed may already have an
    # intraday price frozen into its newest row. Adopt it here so it re-fetches.
    _migrate_legacy_price_cache(paths, cached, fetched_cols)
    _record_quote_kinds(paths, cached, market_open, initialize=True)
    kinds = _quote_kinds(paths)
    targets = {t: (_session_today(t) - pd.Timedelta(days=1) if _crypto(t) else close_date)
               for t in all_tickers}
    pending = {}
    for t in fetched_cols:
        for day in cached[t].dropna().index:
            if kinds.get(t + "|" + day.strftime("%Y-%m-%d")) == "intraday" and day.date() <= targets[t]:
                pending.setdefault(t, []).append(day)
    def settled_dates(ticker):
        if ticker not in cached:
            return []
        return [d for d in cached[ticker].dropna().index
                if kinds.get(ticker + "|" + d.strftime("%Y-%m-%d")) in (
                    {"close"} if uses_event_ledger else {"close", "legacy"})]

    event_history_incomplete = False
    if uses_event_ledger and earliest is not None:
        # Replay needs a settled price for every market ticker on or before the
        # first transaction date. A cache that starts later can look current but
        # cannot correctly price the first contribution or decision benchmark.
        for ticker, required_date in event_price_dates.items():
            if ticker not in cached.columns:
                event_history_incomplete = True
                break
            settled = pd.to_numeric(cached[ticker], errors="coerce").dropna()
            settled = settled.loc[settled.index.isin(settled_dates(ticker))]
            prior = settled.loc[settled.index <= required_date]
            if prior.empty or (required_date.date() - prior.index[-1].date()).days > 7:
                event_history_incomplete = True
                break

    cache_current = bool(event_current_tickers) and all(
        settled_dates(t) and max(settled_dates(t)).date() >= targets[t]
        for t in event_current_tickers
    ) and not event_history_incomplete and not pending

    # Crypto remains live outside equity hours, including weekends. Reconcile
    # returned prior bars too, rather than discarding everything except today.
    live_requested = market_open or any(_crypto(t) for t in event_current_tickers)
    if cache_current and not live_requested:
        return {"status": "ok", "last_updated": _set_last_updated(paths, close_date=close_date)}

    requested = set(all_tickers if event_history_incomplete else event_current_tickers) | set(pending)
    if event_history_incomplete or any(not settled_dates(t) for t in requested):
        if earliest is None:
            return {"status": "incomplete", "message": "No history start date"}
        start = (earliest - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    else:
        starts = [max(settled_dates(t)) + pd.Timedelta(days=1) for t in requested]
        starts.extend(d for days in pending.values() for d in days)
        if live_requested:
            starts.append(pd.Timestamp(min(_session_today(t) for t in requested)) - pd.Timedelta(days=4))
        start = min(starts).strftime("%Y-%m-%d")

    # Fetch with retries for missing tickers
    remaining = sorted(requested)
    new_frames = []

    for attempt in range(max_retries):
        if not remaining:
            break
        try:
            data = yf.download(
                remaining, start=start, progress=False,
                auto_adjust=False,
            )
            if data.empty:
                break
            close = data["Close"]
            if isinstance(close, pd.Series):
                close = close.to_frame(name=remaining[0])
            close = close.apply(pd.to_numeric, errors="coerce")
            close = close.where((close > 0) & (close < float("inf")))
            new_frames.append(close)
            # Check which tickers got data for the close_date
            got = [
                t for t in remaining
                if t in close.columns
                and close[t].last_valid_index() is not None
                and (
                    t not in event_current_tickers
                    or close[t].last_valid_index().date() >= (
                        _session_today(t) if live_requested and (_crypto(t) or market_open)
                        else targets[t]
                    )
                )
            ]
            # A later quote cannot settle a missing earlier provisional cell.
            got = [t for t in got if all(
                d in close.index and pd.notna(close.loc[d, t])
                for d in pending.get(t, [])
            )]
            remaining = [t for t in remaining if t not in got]
        except Exception:
            pass
        if remaining and attempt < max_retries - 1:
            time.sleep(3)

    if cache_current and remaining and not allow_partial_history:
        return {"status": "incomplete", "failed_tickers": sorted(remaining)}

    history_gaps = []
    # Historical gaps do not invalidate independently usable current prices or
    # benchmark history. The staged refresh validates accounting before publishing.
    # Validate historical coverage before writing anything. A nonempty response
    # may contain only today's quote, even when a full history was requested.
    if uses_event_ledger:
        candidate = cached.copy()
        for frame in new_frames:
            candidate = frame.combine_first(candidate)
        gaps = []
        for ticker, required_date in event_price_dates.items():
            values = (pd.to_numeric(candidate[ticker], errors="coerce")
                      if ticker in candidate else pd.Series(dtype=float, index=pd.DatetimeIndex([])))
            values = values[(values > 0) & (values < float("inf"))].dropna()
            prior = values.loc[values.index <= required_date]
            if prior.empty or (required_date.date() - prior.index.max().date()).days > 7:
                gaps.append(f"{ticker}: no historical price on or before {required_date.date()}")
        history_gaps = gaps
        if gaps and not allow_partial_history:
            return {"status": "incomplete", "failed_tickers": sorted(set(remaining) | {
                gap.split(":", 1)[0] for gap in gaps
            }), "message": "; ".join(gaps)}

    # Merge new data into cache
    if new_frames:
        new_data = pd.DataFrame()
        for frame in new_frames:
            frame = frame[~frame.index.duplicated(keep="last")]
            new_data = frame.combine_first(new_data)
        if not new_data.empty:
            if not cached.empty:
                # Fresh provider values win where present, while cached/manual
                # values survive on the same date in columns absent from the
                # download (notably Fundrise prices beside VOO/QQQ closes).
                combined = new_data.combine_first(cached).sort_index()
            else:
                combined = new_data.sort_index()
            os.makedirs(os.path.dirname(paths["price_history"]), exist_ok=True)
            combined.to_csv(paths["price_history"])
            _record_quote_kinds(paths, new_data, market_open)
            _sync_provisional_dates(paths)
            # Rebuild the chart cache so it reflects corrected closes.
            # The coordinator invalidates derived values via input fingerprints.

    if uses_event_ledger and new_frames:
        _write_price_policy(paths)
    if remaining or history_gaps:
        failed = sorted(set(remaining) | {gap.split(":", 1)[0] for gap in history_gaps})
        return {"status": "incomplete", "failed_tickers": failed,
                "message": "Some price history is missing: " + ", ".join(failed),
                "history_gaps": history_gaps}

    if uses_event_ledger:
        _write_price_policy(paths)
    ts = _set_last_updated(paths, close_date=close_date, intraday=live_requested)
    return {"status": "ok", "last_updated": ts}


def _record_quote_kinds(paths, prices, market_open, initialize=False):
    path = paths.get("price_kinds") or os.path.join(paths["data_dir"], "price_kinds.json")
    kinds = json.loads(open(path).read()) if os.path.exists(path) else {}
    receipts_path = paths.get("price_receipts") or os.path.join(paths["data_dir"], "price_receipts.json")
    receipts = json.loads(open(receipts_path).read()) if os.path.exists(receipts_path) else {}
    received_at = datetime.now(timezone.utc).isoformat()
    provisional = _read_provisional_dates(paths)
    static_tickers = _manual_tickers(paths)
    for ticker in prices:
        if ticker in static_tickers:
            continue
        for day, value in prices[ticker].dropna().items():
            if not (0 < float(value) < float("inf")):
                continue
            day = pd.Timestamp(day).date().isoformat()
            key = ticker + "|" + day
            if initialize:
                kind = "intraday" if day in provisional else ("close" if _price_policy_is_current(paths) else "legacy")
                if _unfinished(ticker, pd.Timestamp(day).date(), market_open):
                    kinds[key] = "intraday"
                else:
                    kinds.setdefault(key, kind)
            else:
                kinds[key] = "intraday" if _unfinished(ticker, pd.Timestamp(day).date(), market_open) else "close"
                receipts[key] = {"value": str(Decimal(str(value)).normalize()), "received_at": received_at}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as stream:
        json.dump(kinds, stream, sort_keys=True)
    if not initialize:
        with open(receipts_path, "w") as stream:
            json.dump(receipts, stream, sort_keys=True)
