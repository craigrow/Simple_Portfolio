"""Opt-in local acceptance run against copies of full ledgers and recorded Yahoo data.

Usage: python scripts/validate_price_outages.py VALIDATION_DIRECTORY
The directory must contain portfolios/ (copied source/cache files) and
captured/provider.pkl (a trusted, locally captured yfinance DataFrame).
Never writes to the application's original portfolio directory or accesses Yahoo.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from datetime import date
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def run(work):
    source = work / 'portfolios'
    expected = {p.parent.name for p in (ROOT / 'portfolios').glob('*/config.json')}
    actual = {p.parent.name for p in source.glob('*/config.json')}
    assert actual == expected, f'Incomplete validation portfolios: {actual} != {expected}'
    original = hashes(source)
    provider = pd.read_pickle(work / 'captured/provider.pkl')
    os.environ['PORTFOLIOS_DIR'] = str(work / 'cases/warm')
    sys.path.insert(0, str(ROOT))
    import portfolio_engine as engine
    from app import app
    app.config['TESTING'] = True
    actions = {}
    for key, field, value_column in [('splits', 'Stock Splits', 'RATIO'), ('dividends', 'Dividends', 'AMOUNT')]:
        rows = {}
        if field in provider.columns.get_level_values(0):
            for ticker in provider[field]:
                values = provider[field][ticker].dropna()
                for timestamp, value in values[values != 0].items():
                    rows[(ticker, timestamp.strftime('%Y-%m-%d'))] = float(value)
        actions[key] = rows

    def download(tickers, start=None, end=None, **kwargs):
        names = [tickers] if isinstance(tickers, str) else tickers
        result = provider.loc[:, provider.columns.get_level_values(1).isin(names)]
        if start is not None:
            result = result.loc[result.index >= pd.Timestamp(start).tz_localize(None)]
        if end is not None:
            result = result.loc[result.index < pd.Timestamp(end).tz_localize(None)]
        return result.dropna(subset=list(result['Close'].columns.map(lambda t: ('Close', t))), how='all')

    def corporate(key, tickers):
        return [[ticker, day, value] for (ticker, day), value in actions[key].items() if ticker in tickers]

    report = []
    for scenario in ['warm', 'april-no-snapshot', 'april-policy-rebuild']:
        destination = work / 'cases' / scenario
        if destination.exists():
            shutil.rmtree(destination)  # Disposable validation output only.
        shutil.copytree(source, destination)
        engine.PORTFOLIOS_DIR = str(destination)
        assert {pid for pid, _ in engine.list_portfolios()} == expected
        if scenario != 'warm':
            for root in destination.iterdir():
                if not root.is_dir():
                    continue
                prices_path = root / 'data/price_history.csv'
                if prices_path.exists():
                    prices = pd.read_csv(prices_path, index_col=0, parse_dates=True)
                    # Preserve VCMoney's authoritative pre-listing source history.
                    if root.name != 'vcmoney_portfolio':
                        prices.loc[:'2026-04-10'].to_csv(prices_path)
                for name in ['accounting_snapshot.json', 'daily_values.csv', 'daily_values.csv.meta']:
                    (root / 'data' / name).unlink(missing_ok=True)
                if scenario == 'april-policy-rebuild' and root.name != 'vcmoney_portfolio':
                    (root / 'data/price_history.policy').unlink(missing_ok=True)
        with patch.object(engine, '_is_market_open', return_value=False), \
             patch.object(engine, '_last_market_close', return_value=date(2026, 11, 16)), \
             patch.object(engine, '_fetch_splits', side_effect=lambda tickers: corporate('splits', tickers)), \
             patch.object(engine, '_fetch_dividends', side_effect=lambda tickers: corporate('dividends', tickers)), \
             patch('portfolio_engine.yf.download', side_effect=download), \
             patch('time.sleep'):
            client = app.test_client()
            for pid, _ in engine.list_portfolios():
                started = time.monotonic()
                paths = engine.get_paths(pid)
                result = client.get('/refresh?portfolio=' + pid).get_json()
                response = client.get('/?portfolio=' + pid + '&auto_refreshed=1')
                html = response.get_data(as_text=True)
                (work / 'cases' / f'{scenario}-{pid}.html').write_text(html)
                snapshot = engine.get_cached_accounting_snapshot(paths)
                history = engine.get_cached_daily_values(paths)
                issues = []
                if response.status_code != 200:
                    issues.append('page failed')
                for feature in ['id="pv-table"', 'id="historyChart"', 'id="shadow-voo"', 'id="shadow-qqq"', 'id="history-table"']:
                    if feature not in html:
                        issues.append('missing ' + feature)
                if 'No holdings data yet' in html or '$nan' in html or '[NaN' in html or ', NaN' in html:
                    issues.append('empty or invalid dashboard')
                if snapshot is None:
                    issues.append('no accounting snapshot')
                else:
                    actual = snapshot['actual']
                    ledger = pd.read_csv(paths['transactions'])
                    if len(actual['lots']) != int((ledger['ACTION'] == 'BUY').sum()):
                        issues.append('snapshot omits source transactions')
                    if snapshot['valuation_date'] < str(ledger.DATE.max()):
                        issues.append('snapshot predates source transactions')
                    if abs(actual['total_value'] - actual['cash'] - sum(h['current_value'] for h in actual['holdings'])) > .02:
                        issues.append('holdings/cash do not reconcile')
                    if history and history[-1]['MAIN'] is not None and abs(history[-1]['MAIN'] - actual['total_value']) > .02:
                        issues.append('chart/snapshot do not reconcile')
                item = {'scenario': scenario, 'portfolio': pid, 'result': result,
                        'issues': issues, 'history_points': len(history),
                        'missing_actual_dates': sum(h['MAIN'] is None for h in history),
                        'holdings': len(snapshot['actual']['holdings']) if snapshot else 0,
                        'seconds': round(time.monotonic() - started, 2)}
                if scenario == 'warm' and not issues:
                    before = json.dumps(snapshot, sort_keys=True)
                    previous_history = history
                    timestamp_path = Path(paths['last_updated'])
                    previous_stamp = timestamp_path.read_bytes() if timestamp_path.exists() else None
                    with patch('portfolio_engine.yf.download', return_value=pd.DataFrame()), \
                         patch.object(engine, '_fetch_splits', return_value=[]), \
                         patch.object(engine, '_fetch_dividends', return_value=[]), \
                         patch.object(engine, '_last_market_close', return_value=date(2026, 11, 17)):
                        outage = client.get('/refresh?portfolio=' + pid).get_json()
                        outage_page = client.get('/?portfolio=' + pid + '&auto_refreshed=1').get_data(as_text=True)
                    if json.dumps(engine.get_cached_accounting_snapshot(paths), sort_keys=True) != before:
                        issues.append('total outage changed accounting')
                    if engine.get_cached_daily_values(paths) != previous_history:
                        issues.append('total outage changed historical values')
                    if (timestamp_path.read_bytes() if timestamp_path.exists() else None) != previous_stamp:
                        issues.append('total outage advanced freshness')
                    if 'id="pv-table"' not in outage_page or 'id="historyChart"' not in outage_page:
                        issues.append('total outage lost dashboard')
                    if outage.get('status') == 'ok':
                        issues.append('total outage reported fresh prices')
                    item['total_outage_checked'] = True
                report.append(item)
                print(json.dumps(item), flush=True)
                (work / 'report.json').write_text(json.dumps(report, indent=2))
    assert hashes(source) == original, 'Validation input files changed'
    return report


if __name__ == '__main__':
    results = run(Path(sys.argv[1]).resolve())
    sys.exit(1 if any(item['issues'] for item in results) else 0)
