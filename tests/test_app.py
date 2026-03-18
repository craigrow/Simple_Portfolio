import os
import sys
import csv
import json
import pytest
from unittest.mock import patch

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
        assert b"No transactions yet." in resp.data

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
        assert b"Total Invested" in resp.data
        assert b"$1,000.00" in resp.data

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
        assert b"Main Portfolio" in resp.data
        assert b"Shadow Portfolio" in resp.data
        assert b"VOO" in resp.data
        assert b"QQQ" in resp.data

    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0, "VOO": 550.0, "QQQ": 450.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_current_value(self, mock_close, mock_prices, client):
        with open(_paths()["transactions"], "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/?portfolio=test_portfolio")
        assert b"Current Value" in resp.data
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
