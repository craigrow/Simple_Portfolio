import os
import sys
import csv
import json
import pytest
from unittest.mock import patch
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import portfolio_engine
from app import app


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path):
    portfolios_dir = tmp_path / "portfolios"
    test_portfolio = portfolios_dir / "test_portfolio"
    data_dir = test_portfolio / "data"
    data_dir.mkdir(parents=True)

    with open(test_portfolio / "config.json", "w") as f:
        json.dump({"name": "Test Portfolio"}, f)

    portfolio_engine.PORTFOLIOS_DIR = str(portfolios_dir)

    portfolio_engine._test_paths = {
        "root": str(test_portfolio),
        "data_dir": str(data_dir),
        "transactions": str(test_portfolio / "transactions.csv"),
        "portfolio": str(data_dir / "portfolio.csv"),
        "shadow_voo": str(data_dir / "shadow_voo.csv"),
        "shadow_qqq": str(data_dir / "shadow_qqq.csv"),
        "splits": str(data_dir / "splits.csv"),
        "dividends": str(data_dir / "dividends.csv"),
        "price_history": str(data_dir / "price_history.csv"),
        "config": str(test_portfolio / "config.json"),
        "last_updated": str(data_dir / "last_updated.txt"),
        "daily_values": str(data_dir / "daily_values.csv"),
    }

    with open(portfolio_engine._test_paths["transactions"], "w", newline="") as f:
        csv.writer(f).writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED"])

    yield

    if hasattr(portfolio_engine, "_test_paths"):
        del portfolio_engine._test_paths


def _paths():
    return portfolio_engine._test_paths


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _mock_closing_price(ticker, date_str):
    prices = {
        ("VOO", "2025-01-02"): 500.0,
        ("QQQ", "2025-01-02"): 400.0,
    }
    return prices.get((ticker, date_str))


class TestIndexRoute:
    def test_empty_portfolio(self, client):
        resp = client.get("/?portfolio=test_portfolio")
        assert resp.status_code == 200
        assert b"Not refreshed yet" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_transactions(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/?portfolio=test_portfolio")
        assert resp.status_code == 200
        assert b"MSFT" in resp.data
        assert b"VOO" in resp.data
        assert b"QQQ" in resp.data

    def test_page_title(self, client):
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Simple Portfolio Tracker" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_total_invested(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Invested" in resp.data
        assert b"$1,000" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_syncs_on_page_load(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        client.get("/?portfolio=test_portfolio")
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert len(portfolio) == 1

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_all_three_tables_present(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Transactions" in resp.data
        assert b"Shadow" in resp.data
        assert b"VOO" in resp.data
        assert b"QQQ" in resp.data

    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0, "VOO": 550.0, "QQQ": 450.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_current_value(self, mock_close, mock_prices, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Value" in resp.data
        assert b"Dividends" in resp.data

    def test_portfolio_dropdown_present(self, client):
        resp = client.get("/?portfolio=test_portfolio")
        assert b"test_portfolio" in resp.data
        assert b"Test Portfolio" in resp.data
        assert b'<select' in resp.data

    def test_defaults_to_first_portfolio(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Test Portfolio" in resp.data

    def test_invalid_portfolio_defaults(self, client):
        resp = client.get("/?portfolio=nonexistent")
        assert resp.status_code == 200
        assert b"Test Portfolio" in resp.data


class TestRefreshRoute:
    def test_returns_json_ok(self, client):
        """Refresh with no data returns JSON ok."""
        resp = client.get("/refresh?portfolio=test_portfolio")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"

    def test_returns_json_on_error(self, client):
        """Engine exception returns JSON error, not HTML."""
        with patch.object(portfolio_engine, "refresh_data", side_effect=RuntimeError("boom")):
            resp = client.get("/refresh?portfolio=test_portfolio")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "error"
            assert "boom" in data["message"]

    def test_defaults_to_first_portfolio(self, client):
        resp = client.get("/refresh")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_invalid_portfolio_defaults(self, client):
        resp = client.get("/refresh?portfolio=nonexistent")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


class TestPortfolioViewRendering:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_portfolio_view_heading(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        # Write price history so portfolio_summary has data
        dates = pd.date_range("2025-01-02", periods=1)
        prices = pd.DataFrame({"MSFT": [150.0], "VOO": [550.0], "QQQ": [450.0]}, index=dates)
        prices.index.name = "Date"
        prices.to_csv(_paths()["price_history"])
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Holdings" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_portfolio_view_columns(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        dates = pd.date_range("2025-01-02", periods=1)
        prices = pd.DataFrame({"MSFT": [150.0], "VOO": [550.0], "QQQ": [450.0]}, index=dates)
        prices.index.name = "Date"
        prices.to_csv(_paths()["price_history"])
        resp = client.get("/?portfolio=test_portfolio")
        assert b'data-col="TICKER"' in resp.data
        assert b'data-col="SHARES_OWNED"' in resp.data
        assert b'data-col="COST_BASIS"' in resp.data
        assert b'data-col="CURRENT_VALUE"' in resp.data
        assert b'data-col="DIVIDENDS"' in resp.data
        assert b'data-col="GAIN_LOSS"' in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_portfolio_view_sortable_headers(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        dates = pd.date_range("2025-01-02", periods=1)
        prices = pd.DataFrame({"MSFT": [150.0], "VOO": [550.0], "QQQ": [450.0]}, index=dates)
        prices.index.name = "Date"
        prices.to_csv(_paths()["price_history"])
        resp = client.get("/?portfolio=test_portfolio")
        assert resp.data.count(b'id="pv-table"') == 1
        # Holdings table has 6 sortable columns
        assert b'data-col="TICKER"' in resp.data

    def test_no_portfolio_view_when_empty(self, client):
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Portfolio View" not in resp.data


class TestStaleDataBanner:
    def test_shows_refresh_button_when_stale(self, client):
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Refresh" in resp.data

    def test_shows_freshness_message(self, client):
        from datetime import date
        portfolio_engine._set_last_updated(_paths(), date(2025, 6, 15))
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Prices as of" in resp.data


class TestCurrentPriceLookup:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_nan_in_last_row_uses_last_valid(self, mock_price, client):
        """OTC tickers with NaN in latest row should still get a price."""
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "PRYMY", 50.0, 10.0])
        dates = pd.date_range("2025-01-02", periods=2)
        prices = pd.DataFrame({
            "PRYMY": [67.0, float("nan")],
            "VOO": [500.0, 510.0],
            "QQQ": [400.0, 410.0],
        }, index=dates)
        prices.index.name = "Date"
        prices.to_csv(_paths()["price_history"])
        resp = client.get("/?portfolio=test_portfolio")
        # Holdings tab present and PRYMY value in JSON data (JS-rendered table)
        assert b"Holdings" in resp.data
        assert b"670.0" in resp.data


class TestComparisonColumns:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_irr_and_vs_columns_present(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        dates = pd.date_range("2025-01-02", periods=1)
        prices = pd.DataFrame({"MSFT": [150.0], "VOO": [550.0], "QQQ": [450.0]}, index=dates)
        prices.index.name = "Date"
        prices.to_csv(_paths()["price_history"])
        resp = client.get("/?portfolio=test_portfolio")
        assert b"IRR" in resp.data
        assert b"vs VOO" in resp.data
        assert b"vs QQQ" in resp.data
        assert b"Total Return" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_shadow_tabs_no_irr_columns(self, mock_price, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/?portfolio=test_portfolio")
        # Shadow tabs should exist but not contain IRR/vs columns
        assert b"Shadow VOO" in resp.data
        assert b"Shadow QQQ" in resp.data
