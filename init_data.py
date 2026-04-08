#!/usr/bin/env python
"""Copy local portfolio data to the persistent disk if empty, then
ensure derived CSVs (portfolio, shadows, prices) are generated."""
import os
import shutil

SRC = os.path.join(os.path.dirname(__file__), "portfolios")
DST = os.environ.get("PORTFOLIOS_DIR", "/data/portfolios")

if not (os.path.exists(DST) and os.listdir(DST)):
    print(f"Copying {SRC} → {DST}")
    shutil.copytree(SRC, DST, dirs_exist_ok=True)
    print("Done copying.")
else:
    # Always sync transactions.csv from repo so new purchases appear on deploy
    import glob
    for txn in glob.glob(os.path.join(SRC, "*/transactions.csv")):
        dst_txn = os.path.join(DST, os.path.relpath(txn, SRC))
        shutil.copy2(txn, dst_txn)
        print(f"Updated {dst_txn}")

# Ensure derived CSVs exist (portfolio.csv, shadows, prices)
os.environ["PORTFOLIOS_DIR"] = DST
import portfolio_engine

for pid, name in portfolio_engine.list_portfolios():
    paths = portfolio_engine.get_paths(pid)
    synced = portfolio_engine.sync(paths)
    print(f"{name}: synced {synced} transactions")
    if not os.path.exists(paths["price_history"]) or os.path.getsize(paths["price_history"]) == 0:
        print(f"{name}: fetching initial price data...")
        portfolio_engine.refresh_data(paths)
        print(f"{name}: refresh complete")
