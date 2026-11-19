"""File adapters and durable observation generations; provider acquisition stays outside replay."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import hashlib
import json
import sqlite3
from functools import lru_cache
import pandas as pd
from pricing import Observation, PriceBook


def read_frame(path, **kwargs):
    if not path or not Path(path).exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, **kwargs)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def instrument_modes(paths):
    config = json.loads(Path(paths['config']).read_text()) if Path(paths.get('config', '')).is_file() else {}
    transactions = read_frame(paths.get('transactions'))
    tickers = set(transactions.get('TICKER', pd.Series(dtype=str)).dropna().astype(str))
    manual = read_frame(paths.get('manual_prices'), dtype=str)
    tickers.update(manual.get('TICKER', pd.Series(dtype=str)).dropna().astype(str))
    modes = {t: 'static' if config.get('manual_pricing') else 'api' for t in tickers}
    modes.update({'VOO': 'api', 'QQQ': 'api'})
    for ticker, mode in config.get('pricing', {}).items():
        if mode not in {'api', 'static'}:
            raise ValueError(f'Invalid pricing mode for {ticker}: {mode}')
        modes[ticker] = mode
    return modes


def fingerprint(paths):
    digest = hashlib.sha256(b'shared-pricing-v2')
    for key in ('transactions', 'manual_prices', 'manual_dividends', 'price_history', 'splits', 'config', 'provisional_prices', 'price_kinds', 'price_receipts'):
        fallback = str(Path(paths.get('data_dir', Path(paths['price_history']).parent)) / (key + '.json')) if key in {'price_kinds', 'price_receipts'} else '/nonexistent'
        p = Path(paths.get(key, fallback))
        digest.update(key.encode())
        if p.is_file():
            digest.update(p.read_bytes())
    policy = Path(paths.get('price_policy') or paths['price_history'] + '.policy')
    if policy.exists():
        digest.update(policy.read_bytes())
    return digest.hexdigest()


def load_book(paths):
    """Read-only, content-addressed adapter; no network or source-file mutation."""
    return _cached_book(tuple(sorted(paths.items())), fingerprint(paths))


@lru_cache(maxsize=8)
def _cached_book(path_items, generation):
    paths = dict(path_items)
    modes = instrument_modes(paths)
    observations = []
    def add(ticker, day, value, source, kind, ref, order):
        try:
            observations.append(Observation(str(ticker), pd.Timestamp(day).date(), Decimal(str(value)),
                                            source, kind, str(ref), order))
        except (ValueError, InvalidOperation):
            # Invalid provider entries must not displace a usable prior observation.
            if source != 'api' and source != 'legacy':
                raise ValueError(f'Invalid {source} price for {ticker} on {day}')
    tx = read_frame(paths.get('transactions'), dtype=str)
    price_column = 'PRICE' if 'PRICE' in tx else 'PURCHASE_PRICE'
    if {'TICKER', 'DATE', price_column}.issubset(tx.columns):
        for i, row in tx.iterrows():
            add(row.TICKER, row.DATE, row[price_column], 'transaction', 'execution',
                row.get('TRANSACTION_ID', f'transaction:{i}'), int(i))
    manual = read_frame(paths.get('manual_prices'), dtype=str)
    if {'TICKER', 'DATE', 'PRICE'}.issubset(manual.columns):
        for i, row in manual.iterrows():
            add(row.TICKER, row.DATE, row.PRICE, 'manual', 'manual', f'manual:{row.TICKER}:{row.DATE}', int(i))
    provisional_path = Path(paths.get('provisional_prices', paths['price_history'] + '.provisional'))
    provisional = set(provisional_path.read_text().splitlines()) if provisional_path.is_file() else set()
    prices = read_frame(paths.get('price_history'), index_col=0)
    policy_path = Path(paths.get('price_policy') or paths['price_history'] + '.policy')
    compatible = policy_path.exists() and policy_path.read_text().strip() == 'unadjusted-close-v1'
    kinds_path = Path(paths.get('price_kinds') or Path(paths.get('data_dir', Path(paths['price_history']).parent)) / 'price_kinds.json')
    kinds = json.loads(kinds_path.read_text()) if kinds_path.exists() else {}
    for ticker in prices:
        if modes.get(ticker) == 'static':
            # Manual caches are projections of authoritative transaction/manual sources.
            continue
        for i, (day, value) in enumerate(prices[ticker].dropna().items()):
            day_str = str(day)[:10]
            # Ambiguous legacy caches remain attributable, never labeled official closes.
            kind = kinds.get(str(ticker)+'|'+day_str, ('intraday' if day_str in provisional else 'close') if compatible else 'legacy')
            add(ticker, day, value, 'api' if kind != 'legacy' else 'legacy', kind, f'cache:{ticker}:{day}', i)
    splits = []
    frame = read_frame(paths.get('splits'))
    for _, row in frame.iterrows():
        ratio = Decimal(str(row.RATIO))
        if not ratio.is_finite() or ratio <= 0:
            raise ValueError('Invalid split ratio')
        splits.append((pd.Timestamp(row.DATE).date(), str(row.TICKER), ratio))
    if fingerprint(paths) != generation:
        raise RuntimeError('Pricing inputs changed while reading; retry with a consistent source generation')
    db_path = Path(paths.get('data_dir', Path(paths['price_history']).parent)) / 'prices.sqlite3'
    if db_path.exists():
        with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as db:
            if db.execute('SELECT 1 FROM generations WHERE id=?', (generation,)).fetchone():
                observations = [Observation(ticker, date.fromisoformat(day), Decimal(value), source, kind, ref, order)
                                for ticker, day, value, source, kind, ref, order in db.execute(
                                    'SELECT instrument,effective_date,value,source,kind,reference,sequence FROM observations WHERE generation=?',
                                    (generation,))]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='receipts'").fetchone():
                receipts = dict(db.execute('SELECT id,recorded_at FROM receipts'))
                from dataclasses import replace
                observations = [replace(o, recorded_at=receipts.get(observation_key(o))) for o in observations]
    receipt_path = Path(paths.get('price_receipts') or Path(paths.get('data_dir', Path(paths['price_history']).parent)) / 'price_receipts.json')
    received = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    from dataclasses import replace
    dated = []
    for obs in observations:
        receipt = received.get(obs.instrument + '|' + obs.effective_date.isoformat(), {})
        # CSV float parsing may differ by a few final binary digits.
        matches = receipt.get('value') is not None and abs(Decimal(receipt['value']) - obs.value) <= abs(obs.value) * Decimal('1e-14')
        stamp = receipt.get('received_at') if obs.source == 'api' and matches else None
        dated.append(replace(obs, received_at=stamp))
    return PriceBook(dated, modes, sorted(splits), generation)


def persist_generation(paths, book, outcome):
    """Atomic, append-only source snapshot; repeated imports are idempotent."""
    db_path = Path(paths.get('data_dir', Path(paths['price_history']).parent)) / 'prices.sqlite3'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS generations (id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS observations (generation TEXT, instrument TEXT, effective_date TEXT, value TEXT, source TEXT, kind TEXT, reference TEXT, sequence INTEGER, PRIMARY KEY(generation,instrument,reference,source))')
        db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, attempted_at TEXT, outcome TEXT)')
        now = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT OR IGNORE INTO generations VALUES (?,?)', (book.generation, now))
        db.executemany('INSERT OR IGNORE INTO receipts VALUES (?,?)',
                       [(observation_key(o), o.recorded_at or now) for rows in book.observations.values() for o in rows])
        db.executemany('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?)', [
            (book.generation, o.instrument, o.effective_date.isoformat(), str(o.value), o.source, o.kind, o.reference, o.order)
            for rows in book.observations.values() for o in rows])
        if outcome is not None:
            db.execute('INSERT INTO attempts(attempted_at,outcome) VALUES (?,?)', (now, json.dumps(outcome, sort_keys=True)))

    _cached_book.cache_clear()


def observation_key(obs):
    fields = (obs.instrument, obs.effective_date.isoformat(), str(obs.value.normalize()), obs.source, obs.kind, obs.reference)
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def source_fingerprint(paths):
    return fingerprint({**paths, 'price_history': '/nonexistent', 'price_policy': '/nonexistent',
                        'splits': '/nonexistent', 'provisional_prices': '/nonexistent', 'price_kinds': '/nonexistent', 'price_receipts': '/nonexistent'})


from contextlib import contextmanager
import fcntl


@contextmanager
def data_lock(paths, exclusive=False):
    directory = Path(paths['data_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.pricing.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
