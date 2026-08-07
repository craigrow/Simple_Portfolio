#!/usr/bin/env python
"""Repair shadow benchmark rows corrupted by the stale forward-fill bug.

Deletes shadow_voo.csv / shadow_qqq.csv per portfolio and lets sync() rebuild
them from transactions.csv using the fixed pricing logic. Snapshots before/after
and reports every row that moved, so the change is a checkable diff rather than
a trust exercise.

Usage:
  python repair_shadows.py --dry-run    # report what would change, write nothing
  python repair_shadows.py --apply      # perform the repair
"""
import argparse
import os
import shutil
import sys
import tempfile

import pandas as pd

import portfolio_engine as pe

SHADOW_KEYS = ["shadow_voo", "shadow_qqq"]

# Rows known to be corrupt, from the audit: an intraday snapshot forward-filled
# onto later dates by the pre-5eef31e cached-price lookup.
KNOWN_BAD = {
    ("crypto_portfolio", "2026-07-28"),
    ("foolish_portfolio", "2026-07-29"),
    ("fundrise_portfolio", "2026-07-24"),
}


def snapshot(paths):
    out = {}
    for key in SHADOW_KEYS:
        p = paths[key]
        out[key] = pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()
    return out


def compare(pid, before, after):
    """Return (changed_rows, added_count, removed_count) for one portfolio."""
    changed, added, removed = [], 0, 0
    for key in SHADOW_KEYS:
        b, a = before[key], after[key]
        if b.empty or a.empty:
            added += max(0, len(a) - len(b))
            continue
        if len(a) != len(b):
            # Row-count change: match on (DATE, TOTAL_VALUE) to find real adds
            bset = list(zip(b.DATE.astype(str), b.TOTAL_VALUE.round(2)))
            aset = list(zip(a.DATE.astype(str), a.TOTAL_VALUE.round(2)))
            from collections import Counter
            bc, ac = Counter(bset), Counter(aset)
            added += sum((ac - bc).values())
            removed += sum((bc - ac).values())
            continue
        m = b.merge(a, left_index=True, right_index=True, suffixes=("_b", "_a"))
        for _, r in m.iterrows():
            if (round(float(r.PURCHASE_PRICE_b), 2) != round(float(r.PURCHASE_PRICE_a), 2)
                    or round(float(r.SHARES_PURCHASED_b), 5) != round(float(r.SHARES_PURCHASED_a), 5)):
                changed.append((key, str(r.DATE_b),
                                round(float(r.PURCHASE_PRICE_b), 2),
                                round(float(r.PURCHASE_PRICE_a), 2)))
    return changed, added, removed


def repair(pid, apply):
    """Rebuild one portfolio's shadows. Returns a report dict."""
    real = pe.get_paths(pid)
    if apply:
        paths = real
    else:
        # Dry run: operate on a throwaway copy so real data is never touched
        srcroot = os.path.abspath(real["root"])
        tmp = tempfile.mkdtemp()
        dst = os.path.join(tmp, pid)
        shutil.copytree(srcroot, dst)
        paths = {k: (v.replace(srcroot, dst) if isinstance(v, str) else v)
                 for k, v in real.items()}

    before = snapshot(paths)
    for key in SHADOW_KEYS:
        if os.path.exists(paths[key]):
            os.remove(paths[key])
    pe.sync(paths)
    after = snapshot(paths)
    changed, added, removed = compare(pid, before, after)

    if not apply:
        shutil.rmtree(os.path.dirname(paths["root"]), ignore_errors=True)

    return {"pid": pid, "changed": changed, "added": added, "removed": removed,
            "rows": {k: len(v) for k, v in after.items()}}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== Shadow row repair — {mode} ===\n")

    unexpected_total = 0
    for pid, _ in pe.list_portfolios():
        rep = repair(pid, args.apply)
        print(f"{pid}: rows={rep['rows']} added={rep['added']} removed={rep['removed']}")
        for key, date, old, new in rep["changed"]:
            tag = "FIX " if (pid, date) in KNOWN_BAD else "drift"
            if tag == "drift":
                unexpected_total += 1
            print(f"   [{tag}] {key} {date}: {old} -> {new}")
        if rep["removed"]:
            print(f"   WARNING: {rep['removed']} row(s) disappeared — investigate")
        print()

    print(f"Rows changed outside the known-bad set: {unexpected_total} "
          f"(expected: 1-cent rounding only)")
    if not args.apply:
        print("\nNo files were modified. Re-run with --apply to perform the repair.")


if __name__ == "__main__":
    main()
