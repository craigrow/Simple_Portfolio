"""Run isolated local UAT: python scripts/local_uat.py live|outage VALIDATED_CACHE_DIRECTORY."""
import os
import sys
import shutil
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
mode = sys.argv[1]
if mode not in {'live', 'outage'}:
    raise SystemExit('Use live or outage')
recorded = Path(sys.argv[2]).resolve()
if not recorded.is_dir():
    raise SystemExit('Validated portfolio copy is missing')
work = Path(tempfile.mkdtemp(prefix=f'portfolio-user-uat-{mode}-'))
shutil.copytree(recorded, work / 'portfolios')
# Validation caches are disposable; always restore the portfolio discovery
# definitions from the project rather than assuming the cache copy is complete.
expected = {cfg.parent.name for cfg in (root / 'portfolios').glob('*/config.json')}
for pid in expected:
    target = work / 'portfolios' / pid
    if not (target / 'transactions.csv').exists():
        raise SystemExit(f'UAT fixture missing transactions for {pid}')
    source = root / 'portfolios' / pid
    for name in ['transactions.csv', 'manual_prices.csv', 'dividends_received.csv']:
        if (source / name).exists() and (source / name).read_bytes() != (target / name).read_bytes():
            raise SystemExit(f'UAT fixture differs from checkout: {pid}/{name}')
    shutil.copy2(source / 'config.json', target / 'config.json')

os.environ['PORTFOLIOS_DIR'] = str(work / 'portfolios')
sys.path.insert(0, str(root))
import pandas as pd
import portfolio_engine as engine
from app import app
app.config['TEMPLATES_AUTO_RELOAD'] = True
assert {pid for pid, _ in engine.list_portfolios()} == expected, 'Incomplete UAT portfolio list'
port = 5003 if mode == 'live' else 5004
print(f'LOCAL UAT mode={mode} data={work} URL=http://127.0.0.1:{port}', flush=True)
with ExitStack() as stack:
    if mode == 'outage':
        stack.enter_context(patch('portfolio_engine.yf.download', return_value=pd.DataFrame()))
        stack.enter_context(patch.object(engine, '_fetch_splits', return_value=[]))
        stack.enter_context(patch.object(engine, '_fetch_dividends', return_value=[]))
        stack.enter_context(patch.object(engine, '_get_closing_price', return_value=None))
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
