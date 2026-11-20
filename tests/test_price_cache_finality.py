"""Regression coverage for crypto sessions and per-security cache finality."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import pytest
import portfolio_engine as engine
import price_provider as provider


def frame(days, values):
    return pd.DataFrame(values, index=pd.to_datetime(days))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'PORTFOLIOS_DIR', str(tmp_path))
    def create(ticker='BTC-USD', sold=False):
        paths = engine.get_paths('test')
        Path(paths['data_dir']).mkdir(parents=True)
        rows = [['b', '2026-11-16', 'BUY', ticker, 10, 1, '']]
        if sold:
            rows.append(['s', '2026-11-17', 'SELL', ticker, 10, 1, 'b'])
        pd.DataFrame(rows, columns=['TRANSACTION_ID','DATE','ACTION','TICKER','PRICE','SHARES','LOT_ID']).to_csv(paths['transactions'], index=False)
        frame(['2026-11-16'], {ticker:[10], 'VOO':[10], 'QQQ':[20]}).to_csv(paths['price_history'])
        provider._write_price_policy(paths)
        provider._write_provisional_dates(paths, set())
        return paths
    return create


def refresh(paths, now, values, market_open=False, close=date(2026,11,17)):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)
    with patch.object(provider, 'datetime', Clock), patch.object(provider.yf, 'download', return_value=pd.concat({'Close':values}, axis=1)) as download:
        result = provider.refresh(paths, close, market_open, max_retries=1, allow_partial_history=True)
    return result, download


def kind(paths, ticker, day):
    return provider._quote_kinds(paths)[ticker+'|'+day]


def test_crypto_after_equity_close_is_corrected_next_day(setup):
    p=setup()
    refresh(p, datetime(2026,11,17,22,tzinfo=timezone.utc), frame(['2026-11-17'], {'BTC-USD':[110.123456789], 'VOO':[11], 'QQQ':[21]}))
    assert kind(p,'BTC-USD','2026-11-17')=='intraday'
    assert kind(p,'VOO','2026-11-17')=='close'
    refresh(p, datetime(2026,11,18,17,tzinfo=timezone.utc), frame(['2026-11-17','2026-11-18'], {'BTC-USD':[120,130], 'VOO':[11,12], 'QQQ':[21,22]}), True)
    assert pd.read_csv(p['price_history'],index_col=0).loc['2026-11-17','BTC-USD']==120
    assert kind(p,'BTC-USD','2026-11-17')=='close'
    assert kind(p,'BTC-USD','2026-11-18')=='intraday'


def test_utc_rollover_does_not_relabel_crypto_to_equity_date(setup):
    p=setup('DOGE-USD')
    refresh(p, datetime(2026,11,18,1,tzinfo=timezone.utc), frame(['2026-11-17','2026-11-18'], {'DOGE-USD':[.081234567,.082345678], 'VOO':[11,None], 'QQQ':[21,None]}))
    assert kind(p,'DOGE-USD','2026-11-17')=='close'
    assert kind(p,'DOGE-USD','2026-11-18')=='intraday'
    assert pd.read_csv(p['price_history'],index_col=0).loc['2026-11-18','DOGE-USD']==pytest.approx(.082345678)


def test_weekend_crypto_refresh(setup):
    p=setup()
    result, download=refresh(p, datetime(2026,11,22,12,tzinfo=timezone.utc), frame(['2026-11-20','2026-11-21','2026-11-22'], {'BTC-USD':[110,120,130], 'VOO':[11,None,None], 'QQQ':[21,None,None]}), close=date(2026,11,20))
    assert download.called and result['status']=='ok'
    assert kind(p,'BTC-USD','2026-11-21')=='close'
    assert kind(p,'BTC-USD','2026-11-22')=='intraday'


@pytest.mark.parametrize('returned', [None, float('nan'), 0, -1, float('inf')])
def test_missing_sold_quote_remains_pending_until_specific_quote_returns(setup, returned):
    p=setup('OLD',sold=True)
    frame(['2026-11-16','2026-11-17'], {'OLD':[10,111], 'VOO':[10,11], 'QQQ':[20,21]}).to_csv(p['price_history'])
    provider._write_provisional_dates(p,{'2026-11-17'})
    values={'VOO':[12], 'QQQ':[22]}
    if returned is not None: values['OLD']=[returned]
    result, download=refresh(p,datetime(2026,11,18,1,tzinfo=timezone.utc),frame(['2026-11-17'],values))
    assert 'OLD' in download.call_args.args[0]
    assert result['status']=='incomplete'
    assert kind(p,'OLD','2026-11-17')=='intraday'
    assert kind(p,'VOO','2026-11-17')=='close'
    assert '2026-11-17' in provider._read_provisional_dates(p)
    result, download=refresh(p,datetime(2026,11,18,2,tzinfo=timezone.utc),frame(['2026-11-17'],{'OLD':[120], 'VOO':[12], 'QQQ':[22]}))
    assert result['status']=='ok'
    assert kind(p,'OLD','2026-11-17')=='close'
    assert '2026-11-17' not in provider._read_provisional_dates(p)


def test_later_quote_cannot_clear_an_older_provisional_hole(setup):
    p=setup('OLD',sold=True)
    frame(['2026-11-16','2026-11-17'], {'OLD':[10,111], 'VOO':[10,11], 'QQQ':[20,21]}).to_csv(p['price_history'])
    provider._write_provisional_dates(p,{'2026-11-17'})
    result, _=refresh(p,datetime(2026,11,19,1,tzinfo=timezone.utc),frame(['2026-11-17','2026-11-18'],{'OLD':[None,130], 'VOO':[12,13], 'QQQ':[22,23]}),close=date(2026,11,18))
    assert result['status']=='incomplete' and 'OLD' in result['failed_tickers']
    assert kind(p,'OLD','2026-11-17')=='intraday'
    assert kind(p,'OLD','2026-11-18')=='close'


def test_live_refresh_merges_corrected_prior_close(setup):
    p=setup('XYZ')
    result, _=refresh(p,datetime(2026,11,17,17,tzinfo=timezone.utc),frame(['2026-11-16','2026-11-17'],{'XYZ':[12,13], 'VOO':[11,12], 'QQQ':[21,22]}),True,close=date(2026,11,16))
    assert result['status']=='ok'
    assert pd.read_csv(p['price_history'],index_col=0).loc['2026-11-16','XYZ']==12
    assert kind(p,'XYZ','2026-11-16')=='close'
    assert kind(p,'XYZ','2026-11-17')=='intraday'
