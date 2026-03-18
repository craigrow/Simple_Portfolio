import os
import sys
import csv
import json
import pytest
from unittest.mock import patch
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import portfolio_engine


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path):
    """Redirect all file paths to a temp portfolio directory for each test."""
    portfolios_dir = tmp_path / "portfolios"
    test_portfolio = portfolios_dir / "test_portfolio"
    data_dir = test_portfolio / "data"
    data_dir.mkdir(parents=True)

    # Write config
    with open(test_portfolio / "config.json", "w") as f:
        json.dump({"name": "Test Portfolio"}, f)

    portfolio_engine.PORTFOLIOS_DIR = str(portfolios_dir)

    # Override get_paths to point at temp dir
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


def _write_transaction(date, ticker, price, shares):
    with open(_paths()["transactions"], "a", newline="") as f:
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


class TestListPortfolios:
    def test_lists_portfolios(self):
        result = portfolio_engine.list_portfolios()
        assert len(result) == 1
        assert result[0] == ("test_portfolio", "Test Portfolio")

    def test_empty_when_no_dir(self, tmp_path):
        portfolio_engine.PORTFOLIOS_DIR = str(tmp_path / "nonexistent")
        assert portfolio_engine.list_portfolios() == []

    def test_multiple_portfolios(self):
        # Create a second portfolio
        second = os.path.join(portfolio_engine.PORTFOLIOS_DIR, "another")
        os.makedirs(second)
        with open(os.path.join(second, "config.json"), "w") as f:
            json.dump({"name": "Another"}, f)
        result = portfolio_engine.list_portfolios()
        assert len(result) == 2
        assert result[0][0] == "another"  # alphabetical


class TestReadCsv:
    def test_creates_file_if_missing(self):
        path = _paths()["portfolio"]
        if os.path.exists(path):
            os.remove(path)
        df = portfolio_engine.read_csv(path)
        assert os.path.exists(path)
        assert list(df.columns) == portfolio_engine.COLUMNS
        assert len(df) == 0

    def test_reads_existing_file(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        df = portfolio_engine.read_csv(_paths()["transactions"])
        assert len(df) == 1


class TestGetNewTransactions:
    def test_all_new_when_portfolio_empty(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        new = portfolio_engine.get_new_transactions(_paths())
        assert len(new) == 1

    def test_no_new_when_synced(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        with patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price):
            portfolio_engine.sync(_paths())
        new = portfolio_engine.get_new_transactions(_paths())
        assert len(new) == 0

    def test_detects_appended_transactions(self):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        with patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price):
            portfolio_engine.sync(_paths())
        _write_transaction("2025-01-03", "AAPL", 200.0, 5.0)
        new = portfolio_engine.get_new_transactions(_paths())
        assert len(new) == 1
        assert new.iloc[0]["TICKER"] == "AAPL"


class TestSync:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_creates_all_entries(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        count = portfolio_engine.sync(_paths())
        assert count == 1

        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert len(portfolio) == 1
        assert portfolio.iloc[0]["TOTAL_VALUE"] == 1000.0

        voo = portfolio_engine.read_csv(_paths()["shadow_voo"])
        assert len(voo) == 1
        assert voo.iloc[0]["PURCHASE_PRICE"] == 500.0
        assert voo.iloc[0]["SHARES_PURCHASED"] == 2.0

        qqq = portfolio_engine.read_csv(_paths()["shadow_qqq"])
        assert len(qqq) == 1
        assert qqq.iloc[0]["PURCHASE_PRICE"] == 400.0
        assert qqq.iloc[0]["SHARES_PURCHASED"] == 2.5

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_no_duplicates(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        portfolio_engine.sync(_paths())
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert len(portfolio) == 1

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_incremental(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        _write_transaction("2025-01-03", "AAPL", 200.0, 5.0)
        count = portfolio_engine.sync(_paths())
        assert count == 1
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert len(portfolio) == 2

    def test_sync_returns_zero_when_nothing_new(self):
        count = portfolio_engine.sync(_paths())
        assert count == 0

    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_fractional_shares(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 333.33, 3.14159)
        portfolio_engine.sync(_paths())
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert portfolio.iloc[0]["SHARES_PURCHASED"] == 3.14159


class TestShadowMissingPrice:
    def test_shadow_skipped_when_price_unavailable(self):
        def _no_prices(ticker, date_str):
            return None

        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        with patch.object(portfolio_engine, "_get_closing_price", side_effect=_no_prices):
            portfolio_engine.sync(_paths())
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert len(portfolio) == 1
        voo = portfolio_engine.read_csv(_paths()["shadow_voo"])
        qqq = portfolio_engine.read_csv(_paths()["shadow_qqq"])
        assert len(voo) == 0
        assert len(qqq) == 0


class TestMultipleTransactions:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_sync_multiple_at_once(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        _write_transaction("2025-01-03", "AAPL", 200.0, 5.0)
        count = portfolio_engine.sync(_paths())
        assert count == 2
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        assert len(portfolio) == 2
        voo = portfolio_engine.read_csv(_paths()["shadow_voo"])
        assert len(voo) == 2
        qqq = portfolio_engine.read_csv(_paths()["shadow_qqq"])
        assert len(qqq) == 2


class TestLoadAll:
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_load_all_returns_three_dataframes(self, mock_price):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        p, v, q = portfolio_engine.load_all(_paths())
        assert len(p) == 1
        assert len(v) == 1
        assert len(q) == 1


class TestLastMarketClose:
    def test_returns_weekday(self):
        close = portfolio_engine._last_market_close()
        assert close.weekday() < 5

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
        assert result == 60.0

    def test_split_before_purchase_ignored(self):
        result = portfolio_engine.get_adjusted_shares("MSFT", 10.0, "2025-01-03", MOCK_SPLITS)
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
        portfolio_engine.sync(_paths())
        portfolio_engine.sync_splits(_paths())
        assert os.path.exists(_paths()["splits"])
        df = pd.read_csv(_paths()["splits"])
        assert len(df) == 1
        assert df.iloc[0]["TICKER"] == "MSFT"

    @patch.object(portfolio_engine, "_fetch_splits", return_value=[])
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_no_refresh_when_fresh(self, mock_price, mock_splits):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        portfolio_engine.sync_splits(_paths())
        mock_splits.reset_mock()
        portfolio_engine.sync_splits(_paths())
        mock_splits.assert_not_called()

    def test_no_fetch_when_no_tickers(self):
        result = portfolio_engine.sync_splits(_paths())
        assert result is False


class TestGetCurrentValues:
    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_calculates_current_value(self, mock_close, mock_prices):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        with open(_paths()["splits"], "w", newline="") as f:
            csv.writer(f).writerow(["TICKER", "DATE", "RATIO"])
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        enriched, value, divs = portfolio_engine.enrich_portfolio(portfolio, paths=_paths())
        assert value == 1500.0
        assert enriched.iloc[0]["CURRENT_SHARES"] == 10.0
        assert enriched.iloc[0]["CURRENT_VALUE"] == 1500.0

    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_current_value_with_splits(self, mock_close, mock_prices):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        with open(_paths()["splits"], "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["TICKER", "DATE", "RATIO"])
            writer.writerow(["MSFT", "2025-06-01", 2.0])
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        enriched, value, divs = portfolio_engine.enrich_portfolio(portfolio, paths=_paths())
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
        assert result == 7.5

    def test_dividend_before_purchase_ignored(self):
        divs = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2024-06-01"], "AMOUNT": [0.75]})
        empty_splits = pd.DataFrame(columns=["TICKER", "DATE", "RATIO"])
        result = portfolio_engine.get_total_dividends("MSFT", 10.0, "2025-01-02", empty_splits, divs)
        assert result == 0.0

    def test_dividend_with_split_before_dividend(self):
        splits = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2025-03-01"], "RATIO": [2.0]})
        divs = pd.DataFrame({"TICKER": ["MSFT"], "DATE": ["2025-06-01"], "AMOUNT": [0.75]})
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
        assert result == 12.5

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
        portfolio_engine.sync(_paths())
        portfolio_engine.sync_dividends(_paths())
        assert os.path.exists(_paths()["dividends"])
        df = pd.read_csv(_paths()["dividends"])
        assert len(df) == 1
        assert df.iloc[0]["TICKER"] == "MSFT"

    @patch.object(portfolio_engine, "_fetch_dividends", return_value=[])
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_no_refresh_when_fresh(self, mock_price, mock_divs):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        portfolio_engine.sync_dividends(_paths())
        mock_divs.reset_mock()
        portfolio_engine.sync_dividends(_paths())
        mock_divs.assert_not_called()

    def test_no_fetch_when_no_tickers(self):
        result = portfolio_engine.sync_dividends(_paths())
        assert result is False


class TestEnrichWithDividends:
    @patch.object(portfolio_engine, "_fetch_current_prices", return_value={"MSFT": 150.0})
    @patch.object(portfolio_engine, "_get_closing_price", side_effect=_mock_closing_price)
    def test_enrich_includes_dividends(self, mock_close, mock_prices):
        _write_transaction("2025-01-02", "MSFT", 100.0, 10.0)
        portfolio_engine.sync(_paths())
        with open(_paths()["splits"], "w", newline="") as f:
            csv.writer(f).writerow(["TICKER", "DATE", "RATIO"])
        with open(_paths()["dividends"], "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["TICKER", "DATE", "AMOUNT"])
            writer.writerow(["MSFT", "2025-06-01", 0.75])
        portfolio = portfolio_engine.read_csv(_paths()["portfolio"])
        enriched, value, divs = portfolio_engine.enrich_portfolio(portfolio, paths=_paths())
        assert enriched.iloc[0]["TOTAL_DIVIDENDS"] == 7.5
        assert divs == 7.5
