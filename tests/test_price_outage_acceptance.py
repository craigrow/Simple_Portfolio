"""Acceptance contract: a provider gap must not replace the normal dashboard."""
import json
from datetime import date
from unittest.mock import patch

import pytest
import pandas as pd
import portfolio_engine as engine
from app import app


@pytest.mark.parametrize("missing_history", [False, True])
def test_missing_one_tickers_history_keeps_dashboard_and_benchmarks(tmp_path, monkeypatch, missing_history):
    monkeypatch.setattr(engine, "PORTFOLIOS_DIR", str(tmp_path))
    root = tmp_path / "outage"
    (root / "data").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"name": "Outage acceptance"}))
    paths = engine.get_paths("outage")
    pd.DataFrame([
        ["B1", "2026-04-07", "BUY", "EMBJ", 60, 10, ""],
        ["B2", "2026-06-09", "BUY", "XYZ", 100, 2, ""],
    ], columns=["TRANSACTION_ID", "DATE", "ACTION", "TICKER", "PRICE", "SHARES", "LOT_ID"]).to_csv(paths["transactions"], index=False)
    pd.DataFrame({"EMBJ": [63], "VOO": [505], "QQQ": [405]}, index=pd.to_datetime(["2026-04-10"])).to_csv(paths["price_history"])
    fetched = pd.DataFrame({
        "EMBJ": [float("nan"), float("nan"), 70] if missing_history else [60, 65, 70],
        "XYZ": [100, 100, 110], "VOO": [500, 550, 600], "QQQ": [400, 450, 500],
    }, index=pd.to_datetime(["2026-04-07", "2026-06-09", "2026-11-16"]))
    fetched.columns = pd.MultiIndex.from_product([["Close"], fetched.columns])
    with patch.object(engine, "sync_splits"), patch.object(engine, "sync_dividends"), \
         patch.object(engine, "_last_market_close", return_value=date(2026, 11, 16)), \
         patch.object(engine, "_is_market_open", return_value=False), \
         patch("portfolio_engine.yf.download", return_value=fetched):
        client = app.test_client()
        result = client.get("/refresh?portfolio=outage").get_json()
        page = client.get("/?portfolio=outage&auto_refreshed=1")
        stats = client.get("/stats?portfolio=outage")
    assert page.status_code == stats.status_code == 200
    html = page.get_data(as_text=True)
    for element in ('id="pv-table"', 'id="historyChart"', 'id="shadow-voo"', 'id="shadow-qqq"', 'id="history-table"'):
        assert element in html, f"Normal dashboard feature lost: {element}"
    assert "$920" in html
    assert "No holdings data yet" not in html
    assert "Historical benchmark prices needed" not in html
    assert "[NaN" not in html and ", NaN" not in html and "$nan" not in html
    snapshot = engine.get_cached_accounting_snapshot(paths)
    assert snapshot["actual"]["total_value"] == 920
    assert snapshot["portfolio_benchmarks"]["VOO"]["total_value"] > 900
    history = engine.get_cached_daily_values(paths)
    assert history[0]["MAIN"] == (None if missing_history else 600)  # No invented market close.
    assert history[0]["VOO"] == 600
    assert history[-1]["MAIN"] == 920
    assert result["status"] == ("incomplete" if missing_history else "ok")
    if missing_history:
        assert result["usable"] is True
    prior_snapshot = snapshot
    prior_history = history
    timestamp_path = root / "data" / "last_updated.txt"
    timestamp = timestamp_path.read_bytes() if timestamp_path.exists() else None
    # A subsequent total provider outage must preserve the same dashboard and
    # numbers, not merely an HTTP 200 or a populated substitute table.
    with patch.object(engine, "sync_splits"), patch.object(engine, "sync_dividends"), \
         patch.object(engine, "_last_market_close", return_value=date(2026, 11, 17)), \
         patch.object(engine, "_is_market_open", return_value=False), \
         patch("portfolio_engine.yf.download", return_value=pd.DataFrame()):
        outage = client.get("/refresh?portfolio=outage").get_json()
        cached_page = client.get("/?portfolio=outage&auto_refreshed=1").get_data(as_text=True)
    assert outage["status"] == "incomplete"
    assert "$920" in cached_page and 'id="historyChart"' in cached_page
    assert "Cached quotes: 2026-11-16" in cached_page
    assert len(outage["message"]) < 200
    assert engine.get_cached_accounting_snapshot(paths) == prior_snapshot
    assert engine.get_cached_daily_values(paths) == prior_history
    assert (timestamp_path.read_bytes() if timestamp_path.exists() else None) == timestamp


def test_split_and_partial_provider_history_preserve_full_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "PORTFOLIOS_DIR", str(tmp_path))
    root = tmp_path / "split-outage"
    (root / "data").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"name": "Split outage"}))
    paths = engine.get_paths("split-outage")
    pd.DataFrame([["B1", "2025-01-02", "BUY", "XYZ", 100, 10, ""]], columns=[
        "TRANSACTION_ID", "DATE", "ACTION", "TICKER", "PRICE", "SHARES", "LOT_ID"
    ]).to_csv(paths["transactions"], index=False)
    pd.DataFrame({"XYZ": [100.0], "VOO": [500.0], "QQQ": [400.0]}, index=pd.to_datetime(["2025-01-02"])).to_csv(paths["price_history"])
    pd.DataFrame([["XYZ", "2025-01-04", 0.1]], columns=["TICKER", "DATE", "AMOUNT"]).to_csv(paths["dividends"], index=False)
    # Force action refresh regardless of the machine's clock.
    import os
    os.utime(paths["dividends"], (0, 0))
    engine._write_price_policy(paths)
    engine._write_provisional_dates(paths, set())
    fetched = pd.DataFrame({"XYZ": [25.0], "VOO": [510.0], "QQQ": [410.0]}, index=pd.to_datetime(["2025-01-10"]))
    fetched.columns = pd.MultiIndex.from_product([["Close"], fetched.columns])
    with patch.object(engine, "_last_market_close", return_value=date(2025, 1, 10)), \
         patch.object(engine, "_is_market_open", return_value=False), \
         patch.object(engine, "_fetch_splits", return_value=[["XYZ", "2025-01-10", 4.0]]), \
         patch.object(engine, "_fetch_dividends", return_value=[]), \
         patch("portfolio_engine.yf.download", return_value=fetched):
        client = app.test_client()
        result = client.get('/refresh?portfolio=split-outage').get_json()
        page = client.get('/?portfolio=split-outage&auto_refreshed=1').get_data(as_text=True)
    assert result['status'] == 'ok'
    snapshot = engine.get_cached_accounting_snapshot(paths)
    assert snapshot['actual']['holdings'][0]['shares'] == 40
    assert snapshot['actual']['total_value'] == 1001
    history = engine.get_cached_daily_values(paths)
    assert history[0]['MAIN'] == 1000
    assert history[-1]['MAIN'] == 1001
    assert all(row['MAIN'] is not None for row in history)
    assert 'id="historyChart"' in page and 'id="shadow-voo"' in page
    assert '$1,001' in page
    cached = pd.read_csv(paths['price_history'], index_col=0)
    assert cached.loc['2025-01-02', 'XYZ'] == 100
    assert len(pd.read_csv(paths['dividends'])) == 1
    summary = [{"TICKER": "XYZ", "SHARES_OWNED": 40}]
    with patch.object(engine, "_is_market_open", return_value=False):
        gainers, losers, _, _ = engine.get_gainers_losers(summary, paths)
    assert gainers[0]["CHANGE"] == 0
    assert losers[0]["PCT"] == 0  # A 4:1 split is not a 75% loss.

    # Once another symbol establishes a newer session, XYZ has no current quote
    # and must not be ranked as a zero-change daily mover.
    cached.loc["2025-01-13", "VOO"] = 511
    cached.to_csv(paths["price_history"])
    with patch.object(engine, "_is_market_open", return_value=False):
        assert engine.get_gainers_losers(summary, paths) == ([], [], [], [])

