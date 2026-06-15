import csv
import json
import os

import init_data


def test_sync_transaction_files_creates_new_portfolio_directory(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    existing = dst / "existing_portfolio"
    crypto = src / "crypto_portfolio"
    existing.mkdir(parents=True)
    crypto.mkdir(parents=True)

    with open(crypto / "transactions.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED"])
        writer.writerow(["2026-05-11", "FETH", "23.15", "1.079913"])
    with open(crypto / "config.json", "w") as f:
        json.dump({"name": "Crypto Portfolio"}, f)

    init_data.sync_transaction_files(str(src), str(dst))

    copied_transactions = dst / "crypto_portfolio" / "transactions.csv"
    copied_config = dst / "crypto_portfolio" / "config.json"
    assert copied_transactions.exists()
    assert copied_config.exists()
    assert os.path.exists(copied_transactions)


def test_seed_empty_derived_files_restores_empty_csv_without_overwriting_data(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src_data = src / "foolish_portfolio" / "data"
    dst_data = dst / "foolish_portfolio" / "data"
    src_data.mkdir(parents=True)
    dst_data.mkdir(parents=True)

    with open(src_data / "portfolio.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"])
        writer.writerow(["2026-05-26", "GLW", "199.4816", "5.51429", "1100.0"])
    with open(dst_data / "portfolio.csv", "w", newline="") as f:
        csv.writer(f).writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"])

    with open(src_data / "shadow_voo.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"])
        writer.writerow(["2026-05-26", "VOO", "690.01", "1.59418", "1100.0"])
    with open(dst_data / "shadow_voo.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"])
        writer.writerow(["2026-05-19", "VOO", "674.59", "1.48235", "999.98"])

    init_data.seed_empty_derived_files(str(src), str(dst))

    with open(dst_data / "portfolio.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1][1] == "GLW"

    with open(dst_data / "shadow_voo.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1][0] == "2026-05-19"
