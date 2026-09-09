from datetime import date

import pandas as pd
import pytest

from portfolio_replay import (
    MarketBook, ReplayError, _xirr, build_history, normalize_transactions, replay,
)
from migrate_transaction_ledgers import migrate_file


def _ledger(rows):
    return pd.DataFrame(rows, columns=[
        "TRANSACTION_ID", "DATE", "ACTION", "TICKER", "PRICE", "SHARES", "LOT_ID",
    ])


def _market(rows, dividends=None, splits=None):
    prices = pd.DataFrame.from_dict(rows, orient="index")
    prices.index = pd.to_datetime(prices.index)
    return MarketBook(
        prices,
        pd.DataFrame(splits or [], columns=["TICKER", "DATE", "RATIO"]),
        pd.DataFrame(dividends or [], columns=["TICKER", "DATE", "AMOUNT"]),
    )


class TestTransactionNormalization:
    def test_legacy_buys_receive_deterministic_ids(self):
        legacy = pd.DataFrame([
            ["2025-02-01", "ABC", 20, 10],
            ["2025-01-01", "XYZ", 10, 10],
        ], columns=["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED"])

        events = normalize_transactions(legacy)

        assert [event["transaction_id"] for event in events] == ["B000002", "B000001"]
        assert all(event["action"] == "BUY" for event in events)

    def test_same_day_sell_sorts_before_buy(self):
        ledger = _ledger([
            ["B000002", "2025-02-01", "BUY", "ABC", 10, 100, ""],
            ["S000001", "2025-02-01", "SELL", "XYZ", 5, 100, "B000001"],
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
        ])

        events = normalize_transactions(ledger)

        assert [event["transaction_id"] for event in events] == ["B000001", "S000001", "B000002"]

    def test_rejects_duplicate_ids(self):
        ledger = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["B000001", "2025-02-01", "BUY", "ABC", 10, 100, ""],
        ])

        with pytest.raises(ReplayError, match="Duplicate TRANSACTION_ID"):
            normalize_transactions(ledger)

    def test_migration_preserves_legacy_values_and_adds_ids(self, tmp_path):
        path = tmp_path / "transactions.csv"
        path.write_text(
            "DATE,TICKER,PURCHASE_PRICE,SHARES_PURCHASED\n"
            "2025-01-01,XYZ,10.25,1.23456789\n"
        )

        status, count = migrate_file(str(path), write=True)
        migrated = pd.read_csv(path, dtype=str).fillna("")

        assert (status, count) == ("migrated", 1)
        assert list(migrated.columns) == [
            "TRANSACTION_ID", "DATE", "ACTION", "TICKER", "PRICE", "SHARES", "LOT_ID",
        ]
        assert migrated.iloc[0].to_dict() == {
            "TRANSACTION_ID": "B000001", "DATE": "2025-01-01", "ACTION": "BUY",
            "TICKER": "XYZ", "PRICE": "10.25", "SHARES": "1.23456789", "LOT_ID": "",
        }


class TestCashAndPortfolioBenchmarks:
    def test_losing_sale_creates_cash_and_never_sells_continuous_benchmark(self):
        ledger = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["S000001", "2025-02-01", "SELL", "XYZ", 5, 100, "B000001"],
            ["B000002", "2025-03-01", "BUY", "ABC", 10, 100, ""],
        ])
        market = _market({
            "2025-01-01": {"XYZ": 10, "ABC": None, "VOO": 100, "QQQ": 200},
            "2025-02-01": {"XYZ": 5, "ABC": None, "VOO": 120, "QQQ": 220},
            "2025-03-01": {"XYZ": 5, "ABC": 10, "VOO": 120, "QQQ": 220},
        })

        result = replay(ledger, market, valuation_date=date(2025, 3, 1))

        assert result["actual"]["cash"] == 0
        assert result["actual"]["total_value"] == 1000
        assert result["actual"]["cumulative_contributions"] == 1500
        assert result["portfolio_benchmarks"]["VOO"]["total_value"] == 1700
        assert result["portfolio_benchmarks"]["VOO"]["cumulative_contributions"] == 1500
        assert not any(
            event["type"] == "SALE_PROCEEDS"
            for event in result["portfolio_benchmarks"]["VOO"]["events"]
        )
        xyz = next(decision for decision in result["decisions"] if decision["lot_id"] == "B000001")
        assert xyz["actual"]["gain_loss"] == -500
        assert xyz["VOO"]["gain_loss"] == 200
        assert xyz["vs_voo"] == -700

    def test_winning_sale_funds_new_buy_without_new_benchmark_contribution(self):
        ledger = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["S000001", "2025-02-01", "SELL", "XYZ", 15, 100, "B000001"],
            ["B000002", "2025-03-01", "BUY", "ABC", 15, 100, ""],
        ])
        market = _market({
            "2025-01-01": {"XYZ": 10, "ABC": None, "VOO": 100, "QQQ": 200},
            "2025-02-01": {"XYZ": 15, "ABC": None, "VOO": 120, "QQQ": 240},
            "2025-03-01": {"XYZ": 15, "ABC": 15, "VOO": 120, "QQQ": 240},
        })

        result = replay(ledger, market, valuation_date=date(2025, 3, 1))

        assert result["actual"]["cumulative_contributions"] == 1000
        assert result["actual"]["total_value"] == 1500
        assert result["portfolio_benchmarks"]["VOO"]["cumulative_contributions"] == 1000
        assert result["portfolio_benchmarks"]["VOO"]["total_value"] == 1200
        assert len(result["portfolio_benchmarks"]["VOO"]["lots"]) == 1
        abc = next(decision for decision in result["decisions"] if decision["lot_id"] == "B000002")
        assert abc["original_investment"] == 1500
        assert abc["VOO"]["starting_shares"] == pytest.approx(12.5)

    def test_actual_dividend_cash_reduces_contribution_and_benchmark_dividend_reinvests(self):
        ledger = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["B000002", "2025-02-01", "BUY", "ABC", 10, 100, ""],
        ])
        market = _market({
            "2025-01-01": {"XYZ": 10, "ABC": None, "VOO": 100, "QQQ": 200},
            "2025-01-15": {"XYZ": 10, "ABC": None, "VOO": 100, "QQQ": 200},
            "2025-02-01": {"XYZ": 10, "ABC": 10, "VOO": 100, "QQQ": 200},
        }, dividends=[
            ["XYZ", "2025-01-15", 1],
            ["VOO", "2025-01-15", 5],
            ["QQQ", "2025-01-15", 10],
        ])

        result = replay(ledger, market, valuation_date=date(2025, 2, 1))

        assert result["actual"]["cumulative_contributions"] == 1900
        assert result["actual"]["cash"] == 0
        assert result["portfolio_benchmarks"]["VOO"]["cumulative_contributions"] == 1900
        assert result["portfolio_benchmarks"]["VOO"]["total_value"] == 1950
        assert len(result["portfolio_benchmarks"]["VOO"]["lots"]) == 2

    def test_rejects_partial_sale(self):
        ledger = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["S000001", "2025-02-01", "SELL", "XYZ", 5, 50, "B000001"],
        ])
        market = _market({
            "2025-01-01": {"XYZ": 10, "VOO": 100, "QQQ": 200},
            "2025-02-01": {"XYZ": 5, "VOO": 120, "QQQ": 220},
        })

        with pytest.raises(ReplayError, match="complete sale requires 100 shares"):
            replay(ledger, market, valuation_date=date(2025, 2, 1))

    def test_split_adjusted_complete_sale(self):
        ledger = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["S000001", "2025-02-01", "SELL", "XYZ", 6, 200, "B000001"],
        ])
        market = _market({
            "2025-01-01": {"XYZ": 10, "VOO": 100, "QQQ": 200},
            "2025-02-01": {"XYZ": 6, "VOO": 120, "QQQ": 220},
        }, splits=[["XYZ", "2025-01-20", 2]])

        result = replay(ledger, market, valuation_date=date(2025, 2, 1))

        assert result["actual"]["cash"] == 1200
        assert result["actual"]["lots"][0]["status"] == "CLOSED"


class TestXirr:
    def test_one_year_return(self):
        result = _xirr([
            (date(2023, 1, 1), -1000),
            (date(2024, 1, 1), 1100),
        ])

        assert result == pytest.approx(0.10, abs=1e-8)

    def test_requires_both_signs(self):
        assert _xirr([(date(2025, 1, 1), -1000)]) is None

    def test_decision_xirr_freezes_on_sale_date(self):
        ledger = _ledger([
            ["B000001", "2024-01-01", "BUY", "XYZ", 10, 100, ""],
            ["S000001", "2025-01-01", "SELL", "XYZ", 11, 100, "B000001"],
        ])
        market = _market({
            "2024-01-01": {"XYZ": 10, "VOO": 100, "QQQ": 200},
            "2025-01-01": {"XYZ": 11, "VOO": 110, "QQQ": 220},
            "2025-06-01": {"XYZ": 25, "VOO": 150, "QQQ": 300},
        })

        result = replay(ledger, market, valuation_date=date(2025, 6, 1))
        decision = result["decisions"][0]

        assert decision["terminal_date"] == "2025-01-01"
        assert decision["actual"]["security_value"] == 1100
        assert decision["VOO"]["security_value"] == 1100
        assert decision["actual"]["xirr"] == pytest.approx(1.1 ** (365 / 366) - 1, abs=1e-8)


class TestBenchmarkPriceDates:
    def test_uses_prior_settled_close_within_seven_days(self):
        ledger = _ledger([
            ["B000001", "2025-01-05", "BUY", "XYZ", 10, 100, ""],
        ])
        market = _market({
            "2025-01-03": {"XYZ": 10, "VOO": 100, "QQQ": 200},
            "2025-01-05": {"XYZ": 10, "VOO": None, "QQQ": None},
        })

        result = replay(ledger, market, valuation_date=date(2025, 1, 5))

        assert result["portfolio_benchmarks"]["VOO"]["lots"][0]["price_date"] == "2025-01-03"

    def test_rejects_stale_transaction_price(self):
        ledger = _ledger([
            ["B000001", "2025-01-20", "BUY", "XYZ", 10, 100, ""],
        ])
        market = _market({
            "2025-01-01": {"XYZ": 10, "VOO": 100, "QQQ": 200},
            "2025-01-20": {"XYZ": 10, "VOO": None, "QQQ": None},
        })

        with pytest.raises(ReplayError, match="within 7 days"):
            replay(ledger, market, valuation_date=date(2025, 1, 20))


class TestHistory:
    def test_optimized_history_matches_point_in_time_replay(self, tmp_path):
        transactions = _ledger([
            ["B000001", "2025-01-01", "BUY", "XYZ", 10, 100, ""],
            ["S000001", "2025-02-01", "SELL", "XYZ", 15, 100, "B000001"],
            ["B000002", "2025-03-01", "BUY", "ABC", 15, 100, ""],
        ])
        prices = pd.DataFrame({
            "XYZ": [10, 15, 15], "ABC": [None, None, 15],
            "VOO": [100, 120, 125], "QQQ": [200, 240, 250],
        }, index=pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"]))
        transactions.to_csv(tmp_path / "transactions.csv", index=False)
        prices.to_csv(tmp_path / "prices.csv")
        paths = {
            "transactions": str(tmp_path / "transactions.csv"),
            "price_history": str(tmp_path / "prices.csv"),
            "splits": str(tmp_path / "missing-splits.csv"),
            "dividends": str(tmp_path / "missing-dividends.csv"),
            "manual_dividends": str(tmp_path / "missing-manual-dividends.csv"),
            "config": str(tmp_path / "missing-config.json"),
        }

        history = build_history(paths)
        market = MarketBook(prices)

        for row in history:
            expected = replay(
                transactions, market, valuation_date=date.fromisoformat(row["DATE"])
            )
            assert row["MAIN"] == expected["actual"]["total_value"]
            assert row["VOO"] == expected["portfolio_benchmarks"]["VOO"]["total_value"]
            assert row["QQQ"] == expected["portfolio_benchmarks"]["QQQ"]["total_value"]
            assert row["CONTRIBUTIONS"] == expected["actual"]["cumulative_contributions"]
