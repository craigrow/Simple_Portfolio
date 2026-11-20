"""Auditable split acquisition. Provider coverage is not independent verification."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import os
import tempfile
import pandas as pd
import yfinance as yf
import pricing_sources

SPOT_ASSETS = {'BTC-USD', 'ETH-USD', 'DOGE-USD', 'ZEC-USD'}


def state_path(paths):
    return Path(paths.get('split_status') or Path(paths['data_dir']) / 'split_status.json')


def read_state(paths):
    try:
        return json.loads(state_path(paths).read_text())
    except (OSError, ValueError):
        return {'version': 1, 'securities': {}}


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def requirements(paths):
    tx = pricing_sources.read_frame(paths.get('transactions'), dtype=str)
    if tx.empty:
        return {}
    modes = pricing_sources.instrument_modes(paths)
    result = {t: {'from': str(rows.DATE.min()), 'applicable': modes.get(t) != 'static' and t not in SPOT_ASSETS}
              for t, rows in tx.groupby('TICKER')}
    for t in ('VOO', 'QQQ'):
        result[t] = {'from': str(tx.DATE.min()), 'applicable': True}
    return result


def fetch_history(ticker, start, through):
    """Use explicit ranges and error reporting; empty history is never no-split evidence."""
    frame = yf.Ticker(ticker).history(start=start, end=(date.fromisoformat(through) + timedelta(days=1)).isoformat(),
                                     auto_adjust=False, actions=True, raise_errors=True, timeout=20)
    return validate_history(ticker, start, through, frame)


def validate_history(ticker, start, through, frame):
    """Validate recorded or live history using the same coverage policy."""
    if frame.empty or 'Close' not in frame or 'Stock Splits' not in frame:
        raise ValueError('Provider returned no usable history or no corporate-action field')
    frame = frame.copy()
    frame.index = pd.to_datetime([x.date() for x in frame.index])
    frame = frame.loc[start:through]
    valid = pd.to_numeric(frame.Close, errors='coerce')
    observed = frame.index[(valid > 0) & (valid < float('inf'))]
    if len(observed) == 0:
        raise ValueError('Provider returned no valid dated prices')
    # US-listed holdings use NYSE session dates. Missing sessions remain reviewable,
    # including suspensions, ticker migrations and holidays on other exchanges.
    import pandas_market_calendars as calendars
    expected = calendars.get_calendar('NYSE').valid_days(start_date=start, end_date=through).tz_localize(None)
    missing = expected.difference(observed)
    missing_actions = observed[frame.loc[observed, 'Stock Splits'].isna()]
    rows = []
    for day, value in frame['Stock Splits'].items():
        if pd.isna(value):
            continue
        ratio = Decimal(str(value))
        if ratio == 0:
            continue
        if not ratio.is_finite() or ratio <= 0 or ratio == 1:
            raise ValueError(f'Invalid split ratio on {day.date()}')
        rows.append([ticker, day.date().isoformat(), float(ratio)])
    return {'rows': rows, 'complete': len(missing) == 0 and len(missing_actions) == 0,
            'missing_action_sessions': [x.date().isoformat() for x in missing_actions],
            'observed_from': observed.min().date().isoformat(), 'observed_through': observed.max().date().isoformat(),
            'history_sha256': hashlib.sha256(frame.to_csv().encode()).hexdigest(),
            'missing_sessions': [x.date().isoformat() for x in missing], 'source': 'Yahoo Finance / yfinance explicit history'}


def event_digest(events, ticker):
    rows = sorted((d, str(v.normalize())) for (t, d), v in events.items() if t == ticker)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def sync(paths, through, fetch=fetch_history, force=False, retries=2):
    through = str(through)
    required = requirements(paths)
    if not required:
        return False
    now = datetime.now(timezone.utc).isoformat()
    state = read_state(paths)
    existing = pricing_sources.read_frame(paths['splits'])
    events = {(str(r.TICKER), str(r.DATE)): Decimal(str(r.RATIO)) for r in existing.itertuples()}
    before = dict(events)
    state['version'] = 1
    securities = state.setdefault('securities', {})
    jobs = []
    for ticker, spec in required.items():
        previous = securities.get(ticker, {})
        if not spec['applicable']:
            securities[ticker] = {'status': 'not_applicable', 'reason': 'Static valuation or spot cryptocurrency', 'required_from': spec['from']}
            continue
        # Full-range checks are repeated each settled session, rather than using file mtime.
        if (not force and previous.get('status') == 'provider_checked'
                and previous.get('covered_from', '9999') <= spec['from']
                and previous.get('covered_through', '') >= through
                and previous.get('event_digest') == event_digest(events, ticker)):
            continue
        jobs.append((ticker, spec))

    def acquire(job):
        ticker, spec = job
        attempts = []
        result = None
        for _ in range(retries):
            try:
                result = fetch(ticker, spec['from'], through)
                if not isinstance(result, dict):
                    # Legacy callers can supply events, but not evidence of coverage.
                    result = {'rows': result, 'complete': False, 'reason': 'Response contains events without coverage evidence'}
                attempts.append({'at': datetime.now(timezone.utc).isoformat(), 'result': 'complete' if result.get('complete') else 'partial'})
                if result.get('complete'):
                    break
            except Exception as exc:
                attempts.append({'at': datetime.now(timezone.utc).isoformat(), 'error': str(exc)[:500]})
        return ticker, spec, result, attempts

    with ThreadPoolExecutor(max_workers=4) as pool:
        acquired = list(pool.map(acquire, jobs))
    for ticker, spec, response, attempts in acquired:
        previous = securities.get(ticker, {})
        record = {**previous, 'required_from': spec['from'], 'requested_through': through,
                  'last_attempt_at': now, 'attempts': attempts, 'status': 'needs_review', 'issues': []}
        fetched = {}
        if response is None:
            record['issues'].append('Split-history request failed; known events retained')
        else:
            record['source'] = response.get('source', 'unverified event response')
            record['reported_events'] = response.get('rows', [])
            for key in ('observed_from', 'observed_through', 'missing_sessions', 'missing_action_sessions', 'history_sha256'):
                record[key] = response.get(key)
            if not response.get('complete'):
                record['issues'].append(response.get('reason', 'Provider prices or action data do not cover every required session'))
            for t, day, raw in response.get('rows', []):
                try:
                    value = Decimal(str(raw))
                    date.fromisoformat(day)
                    if t != ticker or not value.is_finite() or value <= 0 or value == 1 or not spec['from'] <= day <= through:
                        raise ValueError()
                    if day in fetched and fetched[day] != value:
                        raise ValueError()
                    fetched[day] = value
                except (ValueError, ArithmeticError):
                    record['issues'].append('Invalid, conflicting or out-of-range split event')
            for day, value in fetched.items():
                key = (ticker, day)
                if key in events and events[key] != value:
                    record['issues'].append(f'Conflicting ratio on {day}: cached {events[key]}, provider {value}; cached event retained')
                else:
                    events[key] = value
            omitted = [d for (t, d) in before if t == ticker and spec['from'] <= d <= through and d not in fetched]
            if omitted:
                record['issues'].append('Provider omitted known events: ' + ', '.join(sorted(omitted)))
            if response.get('complete') and not record['issues']:
                record.update(status='provider_checked', covered_from=spec['from'], covered_through=through, last_success_at=now)
        record['event_digest'] = event_digest(events, ticker)
        securities[ticker] = record
    audit_log = Path(paths['data_dir']) / 'split_attempts.jsonl'
    if acquired:
        prior_log = audit_log.read_text() if audit_log.exists() else ''
        additions = ''.join(json.dumps({'ticker': ticker, **securities[ticker]}, sort_keys=True) + '\n' for ticker, _, _, _ in acquired)
        atomic_text(audit_log, prior_log + additions)
    output = pd.DataFrame([[t, d, float(v)] for (t, d), v in sorted(events.items())], columns=['TICKER', 'DATE', 'RATIO'])
    if events != before or not Path(paths['splits']).exists():
        atomic_text(paths['splits'], output.to_csv(index=False))
    state['updated_at'] = now
    atomic_text(state_path(paths), json.dumps(state, indent=2, sort_keys=True))
    return events != before


def summary(paths, through):
    securities = read_state(paths).get('securities', {})
    frame = pricing_sources.read_frame(paths['splits'])
    events = {(str(r.TICKER), str(r.DATE)): Decimal(str(r.RATIO)) for r in frame.itertuples()}
    evidence_file = Path(__file__).parent / 'audit' / 'split_evidence.json'
    evidence = json.loads(evidence_file.read_text()).get('events', []) if evidence_file.exists() else []
    rows = []
    for ticker, spec in sorted(requirements(paths).items()):
        record = securities.get(ticker, {})
        status = record.get('status', 'not_checked') if spec['applicable'] else 'not_applicable'
        if status == 'provider_checked' and (record.get('covered_from', '9999') > spec['from'] or record.get('covered_through', '') < str(through)):
            status = 'stale'
        issues = list(record.get('issues', []))
        confirmed = 0
        if status == 'provider_checked' and record.get('event_digest') != event_digest(events, ticker):
            status = 'needs_review'
            issues.append('Split cache differs from the last checked event set')
        for event in evidence:
            if event['ticker'] != ticker or not spec['from'] < event['date'] <= str(through):
                continue
            if events.get((ticker, event['date'])) != Decimal(str(event['ratio'])):
                status = 'needs_review'
                issues.append('Missing or conflicting independently confirmed event: ' + event['date'])
            else:
                confirmed += 1
        rows.append({'ticker': ticker, **record, 'status': status, 'required_from': spec['from'],
                     'issues': issues, 'independently_confirmed_events': confirmed})
    pending = [r['ticker'] for r in rows if r['status'] not in {'provider_checked', 'not_applicable'}]
    return {'rows': rows, 'pending': pending, 'complete': not pending,
            'message': ('Split history needs review for: ' + ', '.join(pending)) if pending else 'Provider split history checked. Independent event evidence and ledger reconciliation are recorded in the split audit.'}
