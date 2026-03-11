import os
import sys
import csv
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import portfolio_engine
from app import app


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    portfolio_engine.DATA_DIR = str(data_dir)
    portfolio_engine.TRANSACTIONS_FILE = str(tmp_path / "transactions.csv")
    portfolio_engine.PORTFOLIO_FILE = str(data_dir / "portfolio.csv")
    portfolio_engine.SHADOW_VOO_FILE = str(data_dir / "shadow_voo.csv")
    portfolio_engine.SHADOW_QQQ_FILE = str(data_dir / "shadow_qqq.csv")
    portfolio_engine.SPLITS_FILE = str(data_dir / "splits.csv")
    portfolio_engine.DIVIDENDS_FILE = str(data_dir / "dividends.csv")

    with open(portfolio_engine.TRANSACTIONS_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED"])

    yield


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
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"No transactions yet." in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_transactions(self, mock_price, client):
        with open(portfolio_engine.TRANSACTIONS_FILE, "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"MSFT" in resp.data
        assert b"VOO" in resp.data
        assert b"QQQ" in resp.data

    def test_page_title(self, client):
        resp = client.get("/")
        assert b"Simple Portfolio Tracker" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_total_invested(self, mock_price, client):
        with open(portfolio_engine.TRANSACTIONS_FILE, "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/")
        assert b"Total Invested" in resp.data
        assert b"$1000.00" in resp.data

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_syncs_on_page_load(self, mock_price, client):
        """New transactions in the CSV are processed when the page loads."""
        with open(portfolio_engine.TRANSACTIONS_FILE, "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        client.get("/")
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert len(portfolio) == 1

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_all_three_tables_present(self, mock_price, client):
        with open(portfolio_engine.TRANSACTIONS_FILE, "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/")
        assert b"Main Portfolio" in resp.data
        assert b"Shadow Portfolio" in resp.data
        assert b"VOO" in resp.data
        assert b"QQQ" in resp.data

    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0, "VOO": 550.0, "QQQ": 450.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_displays_current_value(self, mock_close, mock_prices, client):
        with open(portfolio_engine.TRANSACTIONS_FILE, "a", newline="") as f:
            csv.writer(f).writerow(["2025-01-02", "MSFT", 100.0, 10.0])
        resp = client.get("/")
        assert b"Current Value" in resp.data
        assert b"Dividends" in resp.data
