import csv
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

    init_data.sync_transaction_files(str(src), str(dst))

    copied = dst / "crypto_portfolio" / "transactions.csv"
    assert copied.exists()
    assert os.path.exists(copied)
