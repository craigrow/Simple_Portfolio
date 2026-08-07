import csv
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import portfolio_engine
import repair_shadows


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path, monkeypatch):
    """Point the engine at a temp portfolio holding one corrupt shadow row."""
    portfolios_dir = tmp_path / "portfolios"
    test_portfolio = portfolios_dir / "test_portfolio"
    data_dir = test_portfolio / "data"
    data_dir.mkdir(parents=True)

    with open(test_portfolio / "config.json", "w") as f:
        json.dump({"name": "Test Portfolio"}, f)

    monkeypatch.setattr(portfolio_engine, "PORTFOLIOS_DIR", str(portfolios_dir))

    with open(test_portfolio / "transactions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED"])
        w.writerow(["2025-01-02", "AAPL", 100.0, 1.0])
        w.writerow(["2025-01-03", "AAPL", 100.0, 1.0])

    # Price cache covering both dates, so the rebuild needs no network.
    prices = pd.DataFrame(
        {"VOO": [500.0, 510.0], "QQQ": [400.0, 410.0]},
        index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
    )
    prices.index.name = "Date"
    prices.to_csv(data_dir / "price_history.csv")

    yield tmp_path


def _paths():
    return portfolio_engine.get_paths("test_portfolio")


def _seed_shadows(voo_rows, qqq_rows):
    for key, rows in [("shadow_voo", voo_rows), ("shadow_qqq", qqq_rows)]:
        with open(_paths()[key], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(portfolio_engine.COLUMNS)
            w.writerows(rows)


def _seed_portfolio():
    with open(_paths()["portfolio"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(portfolio_engine.COLUMNS)
        w.writerow(["2025-01-02", "AAPL", 100.0, 1.0, 100.0])
        w.writerow(["2025-01-03", "AAPL", 100.0, 1.0, 100.0])


class TestRepairShadows:
    """Covers the repair tool that rebuilds shadow rows corrupted by the
    pre-fix forward-fill bug (an intraday snapshot carried onto later dates)."""

    def test_apply_replaces_corrupt_price_with_real_close(self):
        _seed_portfolio()
        # 2025-01-03 stored at the 01-02 close — the bug's signature.
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 400.0, 0.25, 100.0]],
        )
        repair_shadows.repair("test_portfolio", apply=True)

        voo = pd.read_csv(_paths()["shadow_voo"])
        bad = voo[voo.DATE == "2025-01-03"].iloc[0]
        assert bad["PURCHASE_PRICE"] == 510.0
        assert bad["SHARES_PURCHASED"] == round(100.0 / 510.0, 5)

    def test_dry_run_leaves_files_untouched(self):
        _seed_portfolio()
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 400.0, 0.25, 100.0]],
        )
        before = open(_paths()["shadow_voo"]).read()
        report = repair_shadows.repair("test_portfolio", apply=False)

        assert open(_paths()["shadow_voo"]).read() == before
        assert report["changed"], "dry run should still report what would change"

    def test_dry_run_reports_the_same_change_apply_makes(self):
        _seed_portfolio()
        seed = (
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 400.0, 0.25, 100.0]],
        )
        _seed_shadows(*seed)
        dry = repair_shadows.repair("test_portfolio", apply=False)
        _seed_shadows(*seed)
        applied = repair_shadows.repair("test_portfolio", apply=True)
        assert dry["changed"] == applied["changed"]

    def test_repair_is_idempotent(self):
        _seed_portfolio()
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 400.0, 0.25, 100.0]],
        )
        repair_shadows.repair("test_portfolio", apply=True)
        second = repair_shadows.repair("test_portfolio", apply=True)
        assert second["changed"] == []
        assert second["added"] == 0
        assert second["removed"] == 0

    def test_correct_rows_are_preserved(self):
        """A clean shadow file must come back byte-identical."""
        _seed_portfolio()
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 510.0, round(100.0 / 510.0, 5), 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 410.0, round(100.0 / 410.0, 5), 100.0]],
        )
        report = repair_shadows.repair("test_portfolio", apply=True)
        assert report["changed"] == []

    def test_leaves_price_history_and_portfolio_untouched(self):
        _seed_portfolio()
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 400.0, 0.25, 100.0]],
        )
        ph_before = open(_paths()["price_history"], "rb").read()
        port_before = open(_paths()["portfolio"], "rb").read()

        repair_shadows.repair("test_portfolio", apply=True)

        assert open(_paths()["price_history"], "rb").read() == ph_before
        assert open(_paths()["portfolio"], "rb").read() == port_before

    def test_missing_shadow_rows_are_restored(self):
        """The crypto case: shadow file short a row vs portfolio."""
        _seed_portfolio()
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0]],
        )
        report = repair_shadows.repair("test_portfolio", apply=True)

        voo = pd.read_csv(_paths()["shadow_voo"])
        assert len(voo) == 2
        assert report["added"] == 2  # one row per shadow file
        assert report["removed"] == 0

    def test_rebuilt_rows_align_with_portfolio(self):
        _seed_portfolio()
        _seed_shadows(
            [["2025-01-02", "VOO", 500.0, 0.2, 100.0],
             ["2025-01-03", "VOO", 500.0, 0.2, 100.0]],
            [["2025-01-02", "QQQ", 400.0, 0.25, 100.0],
             ["2025-01-03", "QQQ", 400.0, 0.25, 100.0]],
        )
        repair_shadows.repair("test_portfolio", apply=True)

        portfolio = pd.read_csv(_paths()["portfolio"])
        for key in ["shadow_voo", "shadow_qqq"]:
            shadow = pd.read_csv(_paths()[key])
            assert portfolio_engine._shadow_rows_align_with_portfolio(
                portfolio, shadow) == len(portfolio) - 1
