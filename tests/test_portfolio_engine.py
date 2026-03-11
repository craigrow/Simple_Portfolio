import os
import sys
import csv
import shutil
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import portfolio_engine


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path):
    """Redirect all file paths to a temp directory for each test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    portfolio_engine.DATA_DIR = str(data_dir)
    portfolio_engine.TRANSACTIONS_FILE = str(tmp_path / "transactions.csv")
    portfolio_engine.PORTFOLIO_FILE = str(data_dir / "portfolio.csv")
    portfolio_engine.SHADOW_VOO_FILE = str(data_dir / "shadow_voo.csv")
    portfolio_engine.SHADOW_QQQ_FILE = str(data_dir / "shadow_qqq.csv")

    # Create transactions file with header
    with open(portfolio_engine.TRANSACTIONS_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED"])

    yield


def _write_transaction(date, ticker, price, shares):
    with open(portfolio_engine.TRANSACTIONS_FILE, "a", newline="") as f:
        csv.writer(f).writerow([date, ticker, price, shares])


def _mock_closing_price(ticker, date_str):
    prices = {
        ("VOO", "2025-01-02"): 500.0,
        ("QQQ", "2025-01-02"): 400.0,
        ("VOO", "2025-01-03"): 510.0,
        ("QQQ", "2025-01-03"): 410.0,
    }
    return prices.get((ticker, date_str))


class TestReadCsv:
    def test_creates_file_if_missing(self):
        path = portfolio_engine.PORTFOLIO_FILE
        if os.path.exists(path):
            os.remove(path)
        df = portfolio_engine.read_csv(path)
        assert os.path.exists(path)
        assert list(df.columns) == portfolio_engine.COLUMNS
        assert len(df) == 0

    def test_reads_existing_file(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        df = portfolio_engine.read_csv(portfolio_engine.TRANSACTIONS_FILE)
        assert len(df) == 1


class TestGetNewTransactions:
    def test_all_new_when_portfolio_empty(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        new = portfolio_engine.get_new_transactions()
        assert len(new) == 1

    def test_no_new_when_synced(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        with patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price):
            portfolio_engine.sync()
        new = portfolio_engine.get_new_transactions()
        assert len(new) == 0

    def test_detects_appended_transactions(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        with patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price):
            portfolio_engine.sync()
        _write_transaction("2025-01-03", "AAPL", 200.0, 5.0)
        new = portfolio_engine.get_new_transactions()
        assert len(new) == 1
        assert new.iloc[0]["TICKER"] == "AAPL"


class TestSync:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_creates_all_entries(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        count = portfolio_engine.sync()
        assert count == 1

        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert len(portfolio) == 1
        assert portfolio.iloc[0]["TOTAL_VALUE"] == 1000.0

        voo = portfolio_engine.read_csv(portfolio_engine.SHADOW_VOO_FILE)
        assert len(voo) == 1
        assert voo.iloc[0]["PURCHASE_PRICE"] == 500.0
        assert voo.iloc[0]["SHARES_PURCHASED"] == 2.0

        qqq = portfolio_engine.read_csv(portfolio_engine.SHADOW_QQQ_FILE)
        assert len(qqq) == 1
        assert qqq.iloc[0]["PURCHASE_PRICE"] == 400.0
        assert qqq.iloc[0]["SHARES_PURCHASED"] == 2.5

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_no_duplicates(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        portfolio_engine.sync()  # second sync should be a no-op
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert len(portfolio) == 1

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_incremental(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        _write_transaction("2025-01-03", "AAPL", 200.0, 5.0)
        count = portfolio_engine.sync()
        assert count == 1
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert len(portfolio) == 2

    def test_sync_returns_zero_when_nothing_new(self):
        count = portfolio_engine.sync()
        assert count == 0

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_fractional_shares(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 333.33, 3.14159)
        portfolio_engine.sync()
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert portfolio.iloc[0]["SHARES_PURCHASED"] == 3.14159


class TestShadowMissingPrice:
    def test_shadow_skipped_when_price_unavailable(self):
        """If VOO/QQQ price is unavailable, shadow row is not created."""
        def _no_prices(ticker, date_str):
            return None

        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        with patch.object(portfolio_engine, "_get_closing_price", side_effect=_no_prices):
            portfolio_engine.sync()
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert len(portfolio) == 1
        voo = portfolio_engine.read_csv(portfolio_engine.SHADOW_VOO_FILE)
        qqq = portfolio_engine.read_csv(portfolio_engine.SHADOW_QQQ_FILE)
        assert len(voo) == 0
        assert len(qqq) == 0


class TestMultipleTransactions:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_multiple_at_once(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        _write_transaction("2025-01-03", "AAPL", 200.0, 5.0)
        count = portfolio_engine.sync()
        assert count == 2
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        assert len(portfolio) == 2
        voo = portfolio_engine.read_csv(portfolio_engine.SHADOW_VOO_FILE)
        assert len(voo) == 2
        qqq = portfolio_engine.read_csv(portfolio_engine.SHADOW_QQQ_FILE)
        assert len(qqq) == 2


class TestLoadAll:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_load_all_returns_three_dataframes(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        p, v, q = portfolio_engine.load_all()
        assert len(p) == 1
        assert len(v) == 1
        assert len(q) == 1
