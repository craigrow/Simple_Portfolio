"""Migrate legacy buy-only transaction CSVs to the versioned BUY/SELL ledger.

Usage:
    venv/bin/python migrate_transaction_ledgers.py --check
    venv/bin/python migrate_transaction_ledgers.py --write
"""

import argparse
import csv
import os
import tempfile

import portfolio_engine
from portfolio_replay import LEDGER_COLUMNS, LEGACY_COLUMNS, normalize_transactions
import pandas as pd


def migrated_rows(path):
    """Return (status, rows) while preserving every legacy source value."""
    with open(path, newline="") as source_file:
        reader = csv.DictReader(source_file)
        fields = tuple(reader.fieldnames or ())
        rows = list(reader)
    if set(LEDGER_COLUMNS).issubset(fields):
        normalize_transactions(pd.DataFrame(rows))
        return "current", rows
    if not set(LEGACY_COLUMNS).issubset(fields):
        raise ValueError(f"{path}: unsupported transaction columns: {fields}")

    result = []
    for position, row in enumerate(rows, start=1):
        result.append({
            "TRANSACTION_ID": f"B{position:06d}",
            "DATE": row["DATE"],
            "ACTION": "BUY",
            "TICKER": row["TICKER"],
            "PRICE": row["PURCHASE_PRICE"],
            "SHARES": row["SHARES_PURCHASED"],
            "LOT_ID": "",
        })
    normalize_transactions(pd.DataFrame(result, columns=LEDGER_COLUMNS))
    return "legacy", result


def migrate_file(path, write=False):
    status, rows = migrated_rows(path)
    if status == "current" or not write:
        return status, len(rows)
    directory = os.path.dirname(path)
    descriptor, temp_path = tempfile.mkstemp(prefix="transactions.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", newline="") as output:
            writer = csv.DictWriter(
                output, fieldnames=LEDGER_COLUMNS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
    return "migrated", len(rows)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate and report without writing")
    mode.add_argument("--write", action="store_true", help="atomically rewrite legacy ledgers")
    args = parser.parse_args()

    for portfolio_id, _ in portfolio_engine.list_portfolios():
        path = portfolio_engine.get_paths(portfolio_id)["transactions"]
        status, count = migrate_file(path, write=args.write)
        print(f"{portfolio_id}: {status} ({count} rows)")


if __name__ == "__main__":
    main()
