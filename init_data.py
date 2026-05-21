#!/usr/bin/env python
"""Copy local portfolio data to the persistent disk if empty, then
ensure derived CSVs (portfolio, shadows, prices) are generated."""
import os
import shutil

SRC = os.path.join(os.path.dirname(__file__), "portfolios")
DST = os.environ.get("PORTFOLIOS_DIR", "/data/portfolios")


def sync_transaction_files(src, dst):
    """Copy repo portfolio definition files into the persistent data directory."""
    import glob
    for path in glob.glob(os.path.join(src, "*", "*.csv")) + glob.glob(os.path.join(src, "*", "config.json")):
        dst_path = os.path.join(dst, os.path.relpath(path, src))
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(path, dst_path)
        print(f"Updated {dst_path}")


def main():
    if not (os.path.exists(DST) and os.listdir(DST)):
        print(f"Copying {SRC} → {DST}")
        shutil.copytree(SRC, DST, dirs_exist_ok=True)
        print("Done copying.")
    else:
        # Always sync repo-defined portfolio files so new portfolios and purchases appear on deploy
        sync_transaction_files(SRC, DST)

    # Ensure derived CSVs exist (portfolio.csv, shadows, prices)
    os.environ["PORTFOLIOS_DIR"] = DST
    import portfolio_engine

    for pid, name in portfolio_engine.list_portfolios():
        paths = portfolio_engine.get_paths(pid)
        try:
            synced = portfolio_engine.sync(paths)
            print(f"{name}: synced {synced} transactions")
        except Exception as e:
            print(f"{name}: sync skipped ({e})")
        if not os.path.exists(paths["price_history"]) or os.path.getsize(paths["price_history"]) == 0:
            try:
                print(f"{name}: fetching initial price data...")
                portfolio_engine.refresh_data(paths)
                print(f"{name}: refresh complete")
            except Exception as e:
                print(f"{name}: refresh skipped ({e})")


if __name__ == "__main__":
    main()
