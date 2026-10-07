#!/usr/bin/env python
"""Copy local portfolio data to the persistent disk if empty, then
ensure derived CSVs (portfolio, shadows, prices) are generated."""
import os
import shutil
import csv

SRC = os.path.join(os.path.dirname(__file__), "portfolios")
SEED_SRC = os.path.join(os.path.dirname(__file__), "seed_data")
DST = os.environ.get("PORTFOLIOS_DIR", "/data/portfolios")


def sync_transaction_files(src, dst):
    """Copy repo portfolio definition files into the persistent data directory."""
    import glob
    for path in glob.glob(os.path.join(src, "*", "*.csv")) + glob.glob(os.path.join(src, "*", "config.json")):
        dst_path = os.path.join(dst, os.path.relpath(path, src))
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(path, dst_path)
        print(f"Updated {dst_path}")


def _csv_has_data_rows(path):
    if not os.path.exists(path):
        return False
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        return any(row for row in reader)


def seed_empty_derived_files(src, dst):
    """Restore generated CSV caches from repo defaults only when persistent copies are empty."""
    import glob
    for path in glob.glob(os.path.join(src, "*", "data", "*.csv")):
        dst_path = os.path.join(dst, os.path.relpath(path, src))
        if _csv_has_data_rows(dst_path):
            if os.path.basename(path) == "price_history.csv":
                # Recover seed-only dates absent from a provider backfill while
                # retaining all live prices on overlapping dates.
                import pandas as pd
                live = pd.read_csv(dst_path, index_col=0, parse_dates=True)
                seed = pd.read_csv(path, index_col=0, parse_dates=True)
                combined = live.combine_first(seed).sort_index()
                if not combined.equals(live):
                    combined.to_csv(dst_path)
            continue
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(path, dst_path)
        print(f"Seeded empty derived file {dst_path}")
        if os.path.basename(path) == "price_history.csv":
            # The policy and provisional dates describe this exact seeded cache.
            # Copy them only with a new cache, never over a live cache's metadata.
            for suffix in ("policy", "provisional"):
                source_metadata = os.path.splitext(path)[0] + "." + suffix
                destination_metadata = os.path.splitext(dst_path)[0] + "." + suffix
                if os.path.exists(source_metadata):
                    shutil.copy2(source_metadata, destination_metadata)


def main():
    if not (os.path.exists(DST) and os.listdir(DST)):
        print(f"Copying {SRC} → {DST}")
        shutil.copytree(SRC, DST, dirs_exist_ok=True)
        print("Done copying.")
    else:
        # Always sync repo-defined portfolio files so new portfolios and purchases appear on deploy
        sync_transaction_files(SRC, DST)
    seed_empty_derived_files(SEED_SRC if os.path.isdir(SEED_SRC) else SRC, DST)

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
