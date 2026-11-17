from datetime import date, datetime
from decimal import Decimal
import json
import sqlite3
from unittest.mock import patch
import pandas as pd
import pytest
from pricing import Observation, PriceBook
import pricing_sources
import portfolio_engine as engine
import portfolio_replay as replay
from app import app


def obs(value, day='2025-01-02', source='transaction', kind='execution', order=0):
    return Observation('XYZ', date.fromisoformat(day), Decimal(str(value)), source, kind, f'{source}:{order}', order)


def test_price_selection_has_no_lookahead_and_retains_origin():
    book = PriceBook([obs(10), obs(20, '2025-02-01', 'manual', 'manual')])
    assert book.resolve('XYZ', '2025-01-01').value is None
    result = book.resolve('XYZ', '2025-01-31')
    assert result.value == 10 and result.source == 'transaction' and result.carried_forward
    assert book.resolve('XYZ', '2025-02-01').value == 20
    assert book.resolve('XYZ', '2025-01-02', 'session_close').value is None


def test_same_date_close_precedence_and_later_execution():
    book = PriceBook([obs(10), obs(11, source='manual', kind='manual'),
                      obs(12, source='api', kind='close'), obs(13, '2025-01-03')])
    assert book.resolve('XYZ', '2025-01-02').value == 12
    assert book.resolve('XYZ', '2025-01-03').value == 13


@pytest.mark.parametrize('mode', ['api', 'static'])
def test_mode_does_not_change_value_or_expire_price(mode):
    book = PriceBook([obs('10.123456789')], {'XYZ': mode})
    result = book.resolve('XYZ', '2026-01-02')
    assert result.value == Decimal('10.123456789')
    assert result.status == ('static' if mode == 'static' else 'carried')


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-Infinity', '0', '-1'])
def test_invalid_prices_rejected(bad):
    with pytest.raises(ValueError):
        obs(bad)


def test_split_normalizes_carried_price_once():
    book = PriceBook([obs(100)], splits=[(date(2025, 2, 1), 'XYZ', Decimal(4))])
    assert book.resolve('XYZ', '2025-01-31').value == 100
    assert book.resolve('XYZ', '2025-02-01').value == 25


def test_date_only_book_rejects_intraday_requests():
    with pytest.raises(ValueError, match='end-of-day'):
        PriceBook([obs(10)]).resolve('XYZ', datetime(2025, 1, 2, 9))


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'PORTFOLIOS_DIR', str(tmp_path))
    root = tmp_path / 'mixed'
    (root / 'data').mkdir(parents=True)
    (root / 'config.json').write_text(json.dumps({'name': 'Mixed', 'pricing': {'PRIVATE': 'static', 'XYZ': 'api'}}))
    p = engine.get_paths('mixed')
    pd.DataFrame([
        ['B1', '2025-01-02', 'BUY', 'PRIVATE', 10, 10, ''],
        ['B2', '2025-01-02', 'BUY', 'XYZ', 20, 10, ''],
    ], columns=['TRANSACTION_ID','DATE','ACTION','TICKER','PRICE','SHARES','LOT_ID']).to_csv(p['transactions'], index=False)
    return p


def test_no_provider_or_cache_needed_for_actual_valuation(paths):
    with patch('portfolio_engine.yf.download', side_effect=AssertionError('No network in rendering')):
        snapshot = replay.build_snapshot(paths, valuation_date='2025-03-01')
        assert snapshot['actual']['total_value'] == 300
        assert snapshot['portfolio_benchmarks']['VOO']['total_value'] is None
        response = app.test_client().get('/?portfolio=mixed&auto_refreshed=1')
        assert response.status_code == 200
        assert b'$300' in response.data and b'Price Updates' in response.data
        assert b'Unavailable' in response.data and b'No holdings data yet' not in response.data


def test_static_api_mix_and_history_share_resolution(paths):
    pd.DataFrame({'XYZ': [21, 22], 'VOO': [100, 110], 'QQQ': [200, 220]},
                 index=pd.to_datetime(['2025-01-02', '2025-02-03'])).to_csv(paths['price_history'])
    engine._write_price_policy(paths)
    pd.DataFrame([['2025-01-15','PRIVATE',12]], columns=['DATE','TICKER','PRICE']).to_csv(paths['manual_prices'], index=False)
    snapshot = replay.build_snapshot(paths)
    assert snapshot['actual']['total_value'] == 340
    history = replay.build_history(paths)
    assert history[0]['MAIN'] == 310
    assert history[-1]['MAIN'] == 340
    assert next(r for r in snapshot['price_updates'] if r['ticker']=='PRIVATE')['source'] == 'manual'
    assert engine._manual_tickers(paths) == {'PRIVATE'}


def test_missing_benchmark_at_first_contribution_never_silently_recovers(paths):
    pd.DataFrame({'VOO':[100], 'QQQ':[200]},index=pd.to_datetime(['2025-02-03'])).to_csv(paths['price_history'])
    engine._write_price_policy(paths)
    snapshot = replay.build_snapshot(paths)
    assert snapshot['actual']['total_value'] == 300
    assert snapshot['portfolio_benchmarks']['VOO']['total_value'] is None
    assert snapshot['portfolio_benchmarks']['VOO']['unavailable_since'] == '2025-01-02'
    assert all(row['VOO'] is None for row in replay.build_history(paths))


def test_transaction_sell_observation_updates_another_open_lot(paths):
    pd.DataFrame([
        ['B1','2025-01-02','BUY','XYZ',10,10,''],
        ['B2','2025-01-02','BUY','XYZ',10,10,''],
        ['S1','2025-02-03','SELL','XYZ',20,10,'B1'],
    ],columns=['TRANSACTION_ID','DATE','ACTION','TICKER','PRICE','SHARES','LOT_ID']).to_csv(paths['transactions'],index=False)
    snapshot = replay.build_snapshot(paths)
    assert snapshot['actual']['cash'] == 200
    assert snapshot['actual']['holdings'][0]['current_value'] == 200
    assert snapshot['actual']['total_value'] == 400


def test_observation_generations_are_idempotent_and_revisions_preserved(paths):
    book = pricing_sources.load_book(paths)
    pricing_sources.persist_generation(paths, book, {'status':'ok'})
    pricing_sources.persist_generation(paths, book, {'status':'ok'})
    db = sqlite3.connect(paths['data_dir']+'/prices.sqlite3')
    assert db.execute('select count(*) from generations').fetchone()[0] == 1
    assert db.execute('select count(*) from observations').fetchone()[0] == 2
    pd.DataFrame([['2025-01-15','PRIVATE',12]],columns=['DATE','TICKER','PRICE']).to_csv(paths['manual_prices'],index=False)
    revised = pricing_sources.load_book(paths)
    pricing_sources.persist_generation(paths,revised,{'status':'ok'})
    assert db.execute('select count(*) from generations').fetchone()[0] == 2
    assert book.resolve('PRIVATE','2025-02-01').value == 10
    assert revised.resolve('PRIVATE','2025-02-01').value == 12
    db.close()


def test_complete_outage_without_cache_keeps_static_and_transaction_values(paths):
    with patch.object(engine,'_fetch_splits',return_value=[]), patch.object(engine,'_fetch_dividends',return_value=[]), \
         patch('portfolio_engine.yf.download',return_value=pd.DataFrame()), patch('time.sleep'):
        result = engine.refresh_data(paths, force=True)
    assert result['usable'] is True
    snapshot = engine.get_cached_accounting_snapshot(paths)
    assert snapshot['actual']['total_value'] == 300
    assert snapshot['portfolio_benchmarks']['VOO']['total_value'] is None


def test_offline_calendar_holidays_and_early_close():
    from market_calendar import last_close, is_open
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('America/New_York')
    assert last_close(datetime(2026,11,26,17,tzinfo=tz)) == date(2026,11,25)
    assert not is_open(datetime(2026,11,27,14,tzinfo=tz))
    assert last_close(datetime(2026,11,27,14,tzinfo=tz)) == date(2026,11,27)


def test_split_with_no_replacement_quote_preserves_portfolio_value(paths):
    pd.DataFrame([['XYZ','2025-02-01',4]],columns=['TICKER','DATE','RATIO']).to_csv(paths['splits'],index=False)
    snapshot = replay.build_snapshot(paths,valuation_date='2025-02-03')
    holding = next(h for h in snapshot['actual']['holdings'] if h['ticker']=='XYZ')
    assert holding['shares'] == 40
    assert holding['current_value'] == 200
    price = next(r for r in snapshot['price_updates'] if r['ticker']=='XYZ')
    assert price['value'] == 5 and price['split_factor'] == '4'


def test_valid_observations_survive_derived_calculation_failure(paths):
    def acquire(p, **kwargs):
        pd.DataFrame({'XYZ':[30]},index=pd.to_datetime(['2025-02-03'])).to_csv(p['price_history'])
        engine._write_price_policy(p)
        return {'status':'ok'}
    with patch.object(engine,'sync_splits'), patch.object(engine,'sync_dividends'), \
         patch.object(engine,'update_prices',side_effect=acquire), \
         patch.object(engine,'compute_daily_values',side_effect=RuntimeError('calculation failed')):
        result = engine.refresh_data(paths)
    assert result['status'] == 'error'
    assert pricing_sources.load_book(paths).resolve('XYZ','2025-02-03').value == 30
    assert not __import__('pathlib').Path(paths['last_updated']).exists()


def test_source_change_during_refresh_is_not_published(paths):
    original = __import__('pathlib').Path(paths['transactions']).read_text()
    def mutate(staged):
        __import__('pathlib').Path(paths['transactions']).write_text(original.replace('20,10','25,10'))
        return {'status':'ok'}
    with patch.object(engine,'_refresh_data_staged',side_effect=mutate):
        result = engine.refresh_data(paths)
    assert result['status'] == 'error' and 'Source data changed' in result['message']


def test_failed_instrument_does_not_remove_other_valid_observations(paths):
    pd.DataFrame({'XYZ':[20, float('nan'), -1, float('inf')]},
                 index=pd.to_datetime(['2025-01-02','2025-01-03','2025-01-06','2025-01-07'])).to_csv(paths['price_history'])
    engine._write_price_policy(paths)
    result = pricing_sources.load_book(paths).resolve('XYZ','2025-01-07')
    assert result.value == 20 and result.effective_date == date(2025,1,2)


def test_legacy_adjustment_basis_is_quarantined_not_treated_as_close(paths):
    pd.DataFrame({'XYZ':[999]},index=pd.to_datetime(['2025-02-03'])).to_csv(paths['price_history'])
    book = pricing_sources.load_book(paths)
    assert book.resolve('XYZ','2025-02-03').value == 20
    assert book.resolve('XYZ','2025-02-03','session_close').value is None
    assert any(o.kind == 'legacy' for o in book.observations['XYZ'])


def test_stored_time_does_not_advance_on_failed_refresh(paths):
    book = pricing_sources.load_book(paths)
    pricing_sources.persist_generation(paths,book,{'status':'ok'})
    first = pricing_sources.load_book(paths).resolve('XYZ','2025-03-01')
    pricing_sources.persist_generation(paths,pricing_sources.load_book(paths),{'status':'incomplete'})
    second = pricing_sources.load_book(paths).resolve('XYZ','2025-03-01')
    assert first.recorded_at is not None and first.recorded_at == second.recorded_at
    assert second.effective_date == date(2025,1,2)


def test_per_ticker_intraday_metadata_does_not_turn_another_ticker_into_a_close(paths):
    pd.DataFrame({'XYZ':[20], 'VOO':[100]},index=pd.to_datetime(['2025-02-03'])).to_csv(paths['price_history'])
    engine._write_price_policy(paths)
    __import__('pathlib').Path(paths['price_kinds']).write_text(json.dumps({'XYZ|2025-02-03':'intraday','VOO|2025-02-03':'close'}))
    book = pricing_sources.load_book(paths)
    assert book.resolve('XYZ','2025-02-03','session_close').value is None
    assert book.resolve('VOO','2025-02-03','session_close').value == 100


def test_pricing_and_accounting_do_not_import_provider_modules():
    import ast
    from pathlib import Path
    for name in ('pricing.py','portfolio_replay.py','pricing_sources.py'):
        tree=ast.parse((Path(__file__).parents[1]/name).read_text())
        imports=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node,ast.ImportFrom):
                imports.append(node.module or '')
        assert not {'yfinance','price_provider','portfolio_engine'} & set(imports)


def test_static_manual_update_then_outage_preserves_snapshot_and_provenance(paths):
    pd.DataFrame({'PRIVATE':[10], 'XYZ':[20], 'VOO':[100], 'QQQ':[200]},
                 index=pd.to_datetime(['2025-01-02'])).to_csv(paths['price_history'])
    pd.DataFrame([['2025-01-15','PRIVATE',12]],columns=['DATE','TICKER','PRICE']).to_csv(paths['manual_prices'],index=False)
    engine._write_price_policy(paths)
    engine._write_provisional_dates(paths,set())
    fetched=pd.DataFrame({'XYZ':[22], 'VOO':[110], 'QQQ':[220]},index=pd.to_datetime(['2025-01-20']))
    fetched.columns=pd.MultiIndex.from_product([['Close'],fetched.columns])
    with patch.object(engine,'sync_splits'), patch.object(engine,'sync_dividends'), \
         patch.object(engine,'_is_market_open',return_value=False), patch('time.sleep'):
        with patch.object(engine,'_last_market_close',return_value=date(2025,1,20)), \
             patch('portfolio_engine.yf.download',return_value=fetched):
            engine.refresh_data(paths)
        before=engine.get_cached_accounting_snapshot(paths)
        assert before['actual']['total_value']==340
        with patch.object(engine,'_last_market_close',return_value=date(2025,1,21)), \
             patch('portfolio_engine.yf.download',return_value=pd.DataFrame()):
            engine.refresh_data(paths)
        assert engine.get_cached_accounting_snapshot(paths)==before


def test_missing_benchmark_does_not_erase_actual_investment_multiples(paths):
    pd.DataFrame([['2025-02-01','XYZ',80]],columns=['DATE','TICKER','PRICE']).to_csv(paths['manual_prices'],index=False)
    stats=engine.get_baseball_stats(paths)
    assert stats['counts']['slugging_bases'] == 3
    assert stats['slugging_percentage'] == 1.5
    assert stats['batting_average'] is None


def test_manual_update_preserves_subcent_precision(paths):
    engine.add_manual_price(paths,'2025-02-01','PRIVATE',0.123456789)
    assert pricing_sources.load_book(paths).resolve('PRIVATE','2025-02-01').value == Decimal('0.123456789')


def test_missing_new_session_quote_does_not_create_zero_movers(paths):
    prices=pd.DataFrame({'XYZ':[20,22]},index=pd.to_datetime(['2025-01-02','2025-01-03']))
    prices.to_csv(paths['price_history'])
    engine._write_price_policy(paths)
    with patch.object(engine,'_baseline_date',return_value=pd.Timestamp('2025-01-03')):
        assert engine.get_gainers_losers([{'TICKER':'XYZ','SHARES_OWNED':10}],paths) == ([],[],[],[])
