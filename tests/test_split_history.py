from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import json
import pandas as pd
import pytest
import split_history as splits
import portfolio_engine as engine
from app import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'PORTFOLIOS_DIR', str(tmp_path))
    root = tmp_path / 'test'; (root / 'data').mkdir(parents=True)
    (root / 'config.json').write_text('{"name":"Split test"}')
    p = engine.get_paths('test')
    pd.DataFrame([['B1','2025-01-02','BUY','XYZ',100,10,'']],columns=['TRANSACTION_ID','DATE','ACTION','TICKER','PRICE','SHARES','LOT_ID']).to_csv(p['transactions'],index=False)
    return p


def complete(ticker, start, through):
    return {'rows': [[ticker,'2025-01-03',2]], 'complete': True, 'source':'test provider'}


def test_recent_empty_file_cannot_hide_missing_history(paths):
    Path(paths['splits']).write_text('TICKER,DATE,RATIO\n')
    calls=[]
    def fetch(t,s,e): calls.append(t); return complete(t,s,e)
    assert splits.sync(paths,'2025-01-06',fetch=fetch)
    assert set(calls)=={'XYZ','VOO','QQQ'}
    assert splits.summary(paths,'2025-01-06')['complete']
    calls.clear(); splits.sync(paths,'2025-01-06',fetch=fetch)
    assert not calls
    # Losing cached events invalidates coverage even if the file was just written.
    Path(paths['splits']).write_text('TICKER,DATE,RATIO\n')
    splits.sync(paths,'2025-01-06',fetch=fetch)
    assert set(calls)=={'XYZ','VOO','QQQ'}


def test_partial_failure_retries_each_security_and_retains_known_events(paths):
    splits.sync(paths,'2025-01-06',fetch=complete)
    original=Path(paths['splits']).read_bytes()
    success=splits.read_state(paths)['securities']['XYZ']['last_success_at']
    calls=[]
    def fail(t,s,e):
        calls.append(t)
        if t=='XYZ':raise RuntimeError('timeout')
        return complete(t,s,e)
    splits.sync(paths,'2025-01-07',fetch=fail)
    assert calls.count('XYZ')==2 and calls.count('VOO')==1
    assert Path(paths['splits']).read_bytes()==original
    status=splits.read_state(paths)['securities']
    assert status['XYZ']['last_success_at']==success
    assert status['XYZ']['covered_through']=='2025-01-06'
    assert status['VOO']['covered_through']=='2025-01-07'
    assert splits.summary(paths,'2025-01-07')['pending']==['XYZ']


def test_omissions_and_conflicts_are_quarantined(paths):
    splits.sync(paths,'2025-01-06',fetch=complete)
    original=Path(paths['splits']).read_bytes()
    def bad(t,s,e):return {'complete':True,'rows': [[t,'2025-01-03',3]] if t=='XYZ' else []}
    splits.sync(paths,'2025-01-07',fetch=bad)
    assert Path(paths['splits']).read_bytes()==original
    status=splits.read_state(paths)['securities']
    assert 'Conflicting ratio' in status['XYZ']['issues'][0]
    assert 'omitted known' in status['VOO']['issues'][0]
    assert not splits.summary(paths,'2025-01-07')['complete']


def test_partial_history_never_certifies_no_splits(paths):
    def partial(t,s,e): return {'rows':[], 'complete':False, 'missing_sessions':['2025-01-03']}
    splits.sync(paths,'2025-01-06',fetch=partial)
    assert all('last_success_at' not in x for x in splits.read_state(paths)['securities'].values())


def test_expanded_ledger_range_requires_new_check(paths):
    splits.sync(paths,'2025-01-06',fetch=complete)
    tx=pd.read_csv(paths['transactions']); tx.loc[0,'DATE']='2024-01-02';tx.to_csv(paths['transactions'],index=False)
    calls=[]
    splits.sync(paths,'2025-01-06',fetch=lambda t,s,e:(calls.append(s) or complete(t,s,e)))
    assert calls==['2024-01-02']*3


def test_empty_provider_history_is_failure_not_no_events():
    with patch('split_history.yf.Ticker') as ticker:
        ticker.return_value.history.return_value=pd.DataFrame()
        with pytest.raises(ValueError,match='no usable history'):
            splits.fetch_history('XYZ','2025-01-02','2025-01-06')


def test_missing_middle_session_is_partial_even_when_endpoints_exist():
    frame=pd.DataFrame({'Close':[100,101], 'Stock Splits':[0,0]},index=pd.to_datetime(['2025-01-02','2025-01-06']))
    with patch('split_history.yf.Ticker') as ticker:
        ticker.return_value.history.return_value=frame
        result=splits.fetch_history('XYZ','2025-01-02','2025-01-06')
    assert not result['complete'] and result['missing_sessions']==['2025-01-03']


def test_dashboard_exposes_unchecked_split_history_without_fetching(paths):
    with patch('split_history.yf.Ticker',side_effect=AssertionError('No network on render')):
        response=app.test_client().get('/?portfolio=test&auto_refreshed=1')
    assert response.status_code==200
    assert b'Split history needs review' in response.data
    assert b'id="split-coverage"' in response.data
    assert b'not checked' in response.data


def test_quote_success_cannot_hide_split_failure(paths):
    with patch.object(engine,'sync'), patch.object(engine,'sync_splits',side_effect=RuntimeError('split timeout')), patch.object(engine,'sync_dividends'), patch.object(engine,'update_prices',return_value={'status':'ok'}):
        result=engine.refresh_data(paths)
    assert result['status']=='incomplete' and result['usable']
    assert result['split_history']['error']=='split timeout'
    assert 'Split history needs review' in result['message']


def test_forward_and_reverse_events_reconcile_whole_lot_sales():
    from scripts.audit_splits import quantities
    tx=pd.DataFrame([
        ['B1','2025-01-02','BUY','XYZ','10',''],
        ['B2','2025-01-04','BUY','XYZ','3',''],
        ['S1','2025-01-07','SELL','XYZ','20','B1'],
    ],columns=['TRANSACTION_ID','DATE','ACTION','TICKER','SHARES','LOT_ID'])
    events=[{'TICKER':'XYZ','DATE':'2025-01-03','RATIO':2}, {'TICKER':'XYZ','DATE':'2025-01-08','RATIO':0.5}]
    assert quantities(tx,events,'2025-01-06')['XYZ']==23
    assert quantities(tx,events,'2025-01-09')['XYZ']==Decimal('1.5')


def test_price_jump_is_explained_by_split_on_missing_quote_day(paths):
    from scripts.audit_splits import anomalies
    pd.DataFrame({'XYZ':[100,25]},index=['2025-01-02','2025-01-06']).to_csv(paths['price_history'])
    required={'XYZ':{'from':'2025-01-02','applicable':True}}
    assert len(anomalies(paths,required,[]))==1
    assert anomalies(paths,required,[{'TICKER':'XYZ','DATE':'2025-01-03','RATIO':4}])==[]


def test_missing_independently_confirmed_event_blocks_coverage(paths):
    tx=pd.read_csv(paths['transactions']);tx['TICKER']='CRWD';tx.to_csv(paths['transactions'],index=False)
    def empty(t,s,e):return {'rows':[],'complete':True}
    splits.sync(paths,'2026-11-18',fetch=empty)
    status=splits.summary(paths,'2026-11-18')
    assert 'CRWD' in status['pending']
    row=next(x for x in status['rows'] if x['ticker']=='CRWD')
    assert any('independently confirmed' in i for i in row['issues'])


def test_prices_without_action_values_are_not_verified_no_split_history():
    frame=pd.DataFrame({'Close':[100,101,102], 'Stock Splits':[0,float('nan'),0]},index=pd.to_datetime(['2025-01-02','2025-01-03','2025-01-06']))
    result=splits.validate_history('XYZ','2025-01-02','2025-01-06',frame)
    assert not result['complete']
    assert result['missing_action_sessions']==['2025-01-03']
