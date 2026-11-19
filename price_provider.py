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
    provisional_dates = _read_provisional_dates(paths)
    _record_quote_kinds(paths, cached, market_open, initialize=True)
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
            settled = settled[
                [d.strftime("%Y-%m-%d") not in provisional_dates for d in settled.index]
            ]
            prior = settled.loc[settled.index <= required_date]
            if prior.empty or (required_date.date() - prior.index[-1].date()).days > 7:
                event_history_incomplete = True
                break

    if uses_event_ledger:
        current_by_ticker = []
        for ticker in event_current_tickers:
            if ticker not in cached.columns:
                current_by_ticker.append(False)
                continue
            settled = pd.to_numeric(cached[ticker], errors="coerce").dropna()
            settled_dates = [
                d for d in settled.index
                if d.strftime("%Y-%m-%d") not in provisional_dates
            ]
            current_by_ticker.append(
                bool(settled_dates) and max(settled_dates).date() >= close_date
            )
        cache_current = bool(current_by_ticker) and all(current_by_ticker)
        cache_current = cache_current and not event_history_incomplete
    elif fetched_cols:
        cache_current = all(
            t in cached and any(
                d.date() >= close_date and d.strftime("%Y-%m-%d") not in provisional_dates
                for d in cached[t].dropna().index
            ) for t in all_tickers
        )
    else:
        cache_current = False

    # If cache covers last close and market is open, fetch live intraday prices
    if cache_current and market_open:
        try:
            end = pd.Timestamp.now(tz="America/New_York").normalize() + pd.Timedelta(days=1)
            start = end - pd.Timedelta(days=5)
            data = yf.download(
                all_tickers,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=False,
            )
            if not data.empty:
                close = data["Close"]
                if isinstance(close, pd.Series):
                    close = close.to_frame(name=all_tickers[0])
                close = close.apply(pd.to_numeric, errors="coerce")
                close = close.where((close > 0) & (close < float("inf")))
                live = {}
                today = datetime.now(ZoneInfo("America/New_York")).date()
                for t in all_tickers:
                    if t in close.columns:
                        idx = close[t].last_valid_index()
                        if idx is not None and idx.date() == today:
                            live[t] = float(close[t].loc[idx])
                missing = [t for t in event_current_tickers if t not in live]
                if missing and not allow_partial_history:
                    return {"status": "incomplete", "failed_tickers": sorted(missing)}
                if live:
                    # Write today's intraday row into cache and flag it provisional
                    # so it is re-fetched after the close and never used as a
                    # prior-close baseline.
                    today = pd.Timestamp(datetime.now(ZoneInfo("America/New_York")).date())
                    intraday_row = pd.DataFrame(live, index=[today])
                    hist = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
                    combined = intraday_row.combine_first(hist).sort_index()
                    combined.to_csv(paths["price_history"])
                    _mark_provisional(paths, today.strftime("%Y-%m-%d"))
                    _record_quote_kinds(paths, intraday_row, market_open=True)
                    if missing:
                        return {"status": "incomplete", "failed_tickers": sorted(missing)}
                    _set_last_updated(paths, intraday=True)
                    return {"status": "ok"}
        except Exception as exc:
            return {"status": "incomplete", "message": f"Live prices unavailable: {exc}"}
        return {"status": "incomplete", "message": "Live prices unavailable"}

    # If cache is current and market is closed, nothing to do
    if cache_current:
        return {"status": "ok", "last_updated": _set_last_updated(paths, close_date=close_date)}
    # Backfill: determine start date for historical fetch. Base the resume point
    # on the fetched tickers' last date, not the whole cache — a manual portfolio
    # may hold manual price dates beyond the last benchmark date.
    fetched_last = None
    if fetched_cols and not event_history_incomplete:
        fetched_idx = cached[fetched_cols].dropna(how="all").index
        # Resume from the last *settled* date. Resuming past a provisional row
        # would leave its intraday price in place permanently.
        settled_idx = [d for d in fetched_idx
                       if d.strftime("%Y-%m-%d") not in provisional_dates]
        if len(settled_idx):
            fetched_last = max(settled_idx)
    if fetched_last is not None:
        start = (fetched_last + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    elif earliest is not None:
        start = (earliest - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    else:
        return {"status": "ok", "last_updated": _set_last_updated(paths, close_date=close_date)}

    # Fetch with retries for missing tickers
    remaining = list(all_tickers if event_history_incomplete else event_current_tickers)
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
                    or close[t].last_valid_index().date() >= close_date
                )
            ]
            remaining = [t for t in remaining if t not in got]
        except Exception:
            pass
        if remaining and attempt < max_retries - 1:
            time.sleep(3)

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
            # Any date we just fetched a real close for is no longer provisional.
            if provisional_dates:
                fetched_real = {d.strftime("%Y-%m-%d") for d in new_data.index}
                remaining_provisional = provisional_dates - fetched_real
                if remaining_provisional != provisional_dates:
                    _write_provisional_dates(paths, remaining_provisional)
                    provisional_dates = remaining_provisional
            # A backfill run during market hours brings back today's in-progress
            # bar, which is an intraday price wearing a close's clothing. Flag it
            # so it gets re-fetched and can never serve as a prior-close baseline.
            if market_open:
                today_str = datetime.now(ZoneInfo("America/New_York")).date().strftime("%Y-%m-%d")
                if any(d.strftime("%Y-%m-%d") == today_str for d in new_data.index):
                    _mark_provisional(paths, today_str)
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
    ts = _set_last_updated(paths, close_date=close_date)
    return {"status": "ok", "last_updated": ts}


def _record_quote_kinds(paths, prices, market_open, initialize=False):
    path = paths.get("price_kinds") or os.path.join(paths["data_dir"], "price_kinds.json")
    kinds = json.loads(open(path).read()) if os.path.exists(path) else {}
    receipts_path = paths.get("price_receipts") or os.path.join(paths["data_dir"], "price_receipts.json")
    receipts = json.loads(open(receipts_path).read()) if os.path.exists(receipts_path) else {}
    received_at = datetime.now(timezone.utc).isoformat()
    provisional = _read_provisional_dates(paths)
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
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
                kind = ("intraday" if day in provisional else "close") if _price_policy_is_current(paths) else "legacy"
                kinds.setdefault(key, kind)
            else:
                kinds[key] = "intraday" if market_open and day == today else "close"
                receipts[key] = {"value": str(Decimal(str(value)).normalize()), "received_at": received_at}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as stream:
        json.dump(kinds, stream, sort_keys=True)
    if not initialize:
        with open(receipts_path, "w") as stream:
            json.dump(receipts, stream, sort_keys=True)
