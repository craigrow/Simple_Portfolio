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
