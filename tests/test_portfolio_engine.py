import os
import sys
import csv
import shutil
import pytest
from unittest.mock import patch
import pandas as pd

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
    portfolio_engine.SPLITS_FILE = str(data_dir / "splits.csv")
    portfolio_engine.DIVIDENDS_FILE = str(data_dir / "dividends.csv")
    portfolio_engine.PRICE_HISTORY_FILE = str(data_dir / "price_history.csv")

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


MOCK_SPLITS = pd.DataFrame({
    "TICKER": ["MSFT", "MSFT"],
    "DATE": ["2025-01-03", "2025-06-01"],
    "RATIO": [2.0, 3.0],
})


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


class TestLastMarketClose:
    def test_returns_weekday(self):
        close = portfolio_engine._last_market_close()
        assert close.weekday() < 5  # Mon-Fri

    def test_returns_4pm_et(self):
        close = portfolio_engine._last_market_close()
        assert close.hour == 16
        assert close.minute == 0


class TestGetAdjustedShares:
    def test_no_splits(self):
        empty = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_adjusted_shares("MSFT", 10.0, "2025-01-02", empty)
        assert result == 10.0

    def test_split_after_purchase(self):
        result = portfolio_engine.get_adjusted_shares("MSFT", 10.0, "2025-01-02", MOCK_SPLITS)
        # Two splits after 2025-01-02: 2x on 01-03, 3x on 06-01 => 10 * 2 * 3 = 60
        assert result == 60.0

    def test_split_before_purchase_ignored(self):
        result = portfolio_engine.get_adjusted_shares("MSFT", 10.0, "2025-01-03", MOCK_SPLITS)
        # Only the 06-01 split is after 01-03 => 10 * 3 = 30
        assert result == 30.0

    def test_no_splits_for_ticker(self):
        result = portfolio_engine.get_adjusted_shares("AAPL", 10.0, "2025-01-02", MOCK_SPLITS)
        assert result == 10.0

    def test_fractional_shares_after_split(self):
        result = portfolio_engine.get_adjusted_shares("MSFT", 3.14159, "2025-01-02", MOCK_SPLITS)
        assert result == round(3.14159 * 2.0 * 3.0, 5)


class TestSyncSplits:
    @patch.object(portfolio_engine, "_fetch_splits", return_value=[["MSFT", "2025-01-03", 2.0]])
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_creates_splits_file(self, mock_price, mock_splits):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        portfolio_engine.sync_splits()
        assert os.path.exists(portfolio_engine.SPLITS_FILE)
        df = pd.read_csv(portfolio_engine.SPLITS_FILE)
        assert len(df) == 1
        assert df.iloc[0]["TICKER"] == "MSFT"

    @patch.object(portfolio_engine, "_fetch_splits", return_value=[])
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_no_refresh_when_fresh(self, mock_price, mock_splits):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        portfolio_engine.sync_splits()
        mock_splits.reset_mock()
        portfolio_engine.sync_splits()
        # Should not fetch again since file is fresh
        mock_splits.assert_not_called()

    def test_no_fetch_when_no_tickers(self):
        result = portfolio_engine.sync_splits()
        assert result is False


class TestGetCurrentValues:
    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_calculates_current_value(self, mock_close, mock_prices):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        with open(portfolio_engine.SPLITS_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["TICKER", "DATE", "RATIO"])
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        enriched, value, divs = portfolio_engine.enrich_portfolio(portfolio)
        assert value == 1500.0
        assert enriched.iloc[0]["CURRENT_SHARES"] == 10.0
        assert enriched.iloc[0]["CURRENT_VALUE"] == 1500.0

    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_current_value_with_splits(self, mock_close, mock_prices):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        with open(portfolio_engine.SPLITS_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["TICKER", "DATE", "RATIO"])
            writer.writerow(["MSFT", "2025-06-01", 2.0])
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        enriched, value, divs = portfolio_engine.enrich_portfolio(portfolio)
        assert value == 3000.0
        assert enriched.iloc[0]["CURRENT_SHARES"] == 20.0
        assert enriched.iloc[0]["CURRENT_VALUE"] == 3000.0

    def test_empty_portfolio_returns_zero(self):
        enriched, value, divs = portfolio_engine.enrich_portfolio(pd.DataFrame())
        assert value == 0.0
        assert divs == 0.0


class TestGetTotalDividends:
    def test_no_dividends(self):
        empty_divs = pd.DataFrame(columns=["TICKER", "DATE", "AMOUNT"])
        empty_splits = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", empty_splits, empty_divs)
        assert result == 0.0

    def test_dividend_after_purchase(self):
        divs = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2025-06-01"], "AMOUNT": [0.75]})
        empty_splits = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", empty_splits, divs)
        assert result == 7.5  # 10 shares * $0.75

    def test_dividend_before_purchase_ignored(self):
        divs = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2024-06-01"], "AMOUNT": [0.75]})
        empty_splits = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", empty_splits, divs)
        assert result == 0.0

    def test_dividend_with_split_before_dividend(self):
        splits = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2025-03-01"], "RATIO": [2.0]})
        divs = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2025-06-01"], "AMOUNT": [0.75]})
        # Bought 10 shares on 01-02, 2:1 split on 03-01, dividend on 06-01
        # At dividend time: 20 shares * $0.75 = $15
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", splits, divs)
        assert result == 15.0

    def test_multiple_dividends(self):
        divs = pd.DataFrame({
            "TICKER": ["MSFT", "MSFT"],
            "DATE": ["2025-03-01", "2025-06-01"],
            "AMOUNT": [0.50, 0.75],
        })
        empty_splits = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", empty_splits, divs)
        assert result == 12.5  # (10 * 0.50) + (10 * 0.75)

    def test_wrong_ticker_ignored(self):
        divs = pd.DataFrame({"TICKER": ["AAPL"], "DATE": ["2025-06-01"], "AMOUNT": [0.75]})
        empty_splits = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", empty_splits, divs)
        assert result == 0.0


class TestSyncDividends:
    @patch.object(portfolio_engine, "_fetch_dividends", return_value=[["MSFT", "2025-06-01", 0.75]])
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_creates_dividends_file(self, mock_price, mock_divs):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        portfolio_engine.sync_dividends()
        assert os.path.exists(portfolio_engine.DIVIDENDS_FILE)
        df = pd.read_csv(portfolio_engine.DIVIDENDS_FILE)
        assert len(df) == 1
        assert df.iloc[0]["TICKER"] == "MSFT"

    @patch.object(portfolio_engine, "_fetch_dividends", return_value=[])
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_no_refresh_when_fresh(self, mock_price, mock_divs):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        portfolio_engine.sync_dividends()
        mock_divs.reset_mock()
        portfolio_engine.sync_dividends()
        mock_divs.assert_not_called()

    def test_no_fetch_when_no_tickers(self):
        result = portfolio_engine.sync_dividends()
        assert result is False


class TestEnrichWithDividends:
    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_enrich_includes_dividends(self, mock_close, mock_prices):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync()
        with open(portfolio_engine.SPLITS_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["TICKER", "DATE", "RATIO"])
        with open(portfolio_engine.DIVIDENDS_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["TICKER", "DATE", "AMOUNT"])
            writer.writerow(["MSFT", "2025-06-01", 0.75])
        portfolio = portfolio_engine.read_csv(portfolio_engine.PORTFOLIO_FILE)
        enriched, value, divs = portfolio_engine.enrich_portfolio(portfolio)
        assert enriched.iloc[0]["TOTAL_DIVIDENDS"] == 7.5
        assert divs == 7.5
