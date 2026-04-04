#!/usr/bin/env python
"""Copy local portfolio data to the persistent disk if it's empty.
Run once on first deploy: python init_data.py"""
import os
import shutil

SRC = os.path.join(os.path.dirname(__file__), "portfolios")
DST = os.environ.get("PORTFOLIOS_DIR", "/data/portfolios")

if os.path.exists(DST) and os.listdir(DST):
    print(f"{DST} already has data, skipping.")
else:
    print(f"Copying {SRC} → {DST}")
    shutil.copytree(SRC, DST, dirs_exist_ok=True)
    print("Done.")
