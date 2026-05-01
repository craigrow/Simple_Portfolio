"""Consolidate Fidelity split transactions (whole + fractional) into single rows.

For each portfolio:
1. Backs up all 4 CSV files (transactions, portfolio, shadow_voo, shadow_qqq)
2. Groups rows by DATE+TICKER in transactions.csv
3. Combines groups of 2+ into one row: sum shares, weighted-avg price, sum total_value
4. Applies the same positional merges to portfolio.csv, shadow_voo.csv, shadow_qqq.csv
5. Writes a change log

Usage: python consolidate_transactions.py [--dry-run]
"""
import csv
import os
import shutil
import sys
from datetime import datetime
from collections import defaultdict

PORTFOLIOS_DIR = os.path.join(os.path.dirname(__file__), "portfolios")
DRY_RUN = "--dry-run" in sys.argv


def read_csv(path):
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    return header, rows


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def consolidate_portfolio(portfolio_id):
    root = os.path.join(PORTFOLIOS_DIR, portfolio_id)
    data = os.path.join(root, "data")
    paths = {
        "transactions": os.path.join(root, "transactions.csv"),
        "portfolio": os.path.join(data, "portfolio.csv"),
        "shadow_voo": os.path.join(data, "shadow_voo.csv"),
        "shadow_qqq": os.path.join(data, "shadow_qqq.csv"),
    }

    # Read all files
    txn_header, txn_rows = read_csv(paths["transactions"])
    port_header, port_rows = read_csv(paths["portfolio"])
    voo_header, voo_rows = read_csv(paths["shadow_voo"])
    qqq_header, qqq_rows = read_csv(paths["shadow_qqq"])

    # Find groups to merge: all rows with same DATE+TICKER (may be non-adjacent)
    from collections import OrderedDict
    group_map = OrderedDict()  # key -> [row_indices]
    for i, row in enumerate(txn_rows):
        key = (row[0], row[1])  # (DATE, TICKER)
        if key not in group_map:
            group_map[key] = []
        group_map[key].append(i)
    groups = list(group_map.items())

    # Build merged rows and change log
    log_entries = []
    new_txn = []
    new_port = []
    new_voo = []
    new_qqq = []

    for (date, ticker), indices in groups:
        if len(indices) == 1:
            idx = indices[0]
            new_txn.append(txn_rows[idx])
            new_port.append(port_rows[idx])
            new_voo.append(voo_rows[idx])
            new_qqq.append(qqq_rows[idx])
            continue

        # Merge transactions: weighted avg price, sum shares
        t_rows = [txn_rows[i] for i in indices]
        shares_list = [float(r[3]) for r in t_rows]
        prices_list = [float(r[2]) for r in t_rows]
        total_shares = sum(shares_list)
        wavg_price = sum(p * s for p, s in zip(prices_list, shares_list)) / total_shares
        total_value = sum(float(r[2]) * float(r[3]) for r in t_rows)

        merged_txn = [date, ticker, f"{wavg_price:.2f}", f"{round(total_shares, 6)}"]
        if len(t_rows[0]) > 4:
            merged_txn.append(f"{round(total_value, 2)}")
        new_txn.append(merged_txn)

        # Merge portfolio rows — sum original TOTAL_VALUE to avoid rounding
        p_rows = [port_rows[i] for i in indices]
        p_shares = [float(r[3]) for r in p_rows]
        p_prices = [float(r[2]) for r in p_rows]
        p_total_shares = sum(p_shares)
        p_wavg = sum(p * s for p, s in zip(p_prices, p_shares)) / p_total_shares
        p_total_val = sum(float(r[4]) for r in p_rows)
        new_port.append([date, ticker, f"{p_wavg:.2f}", f"{round(p_total_shares, 6)}", f"{p_total_val}"])

        # Merge shadow VOO — sum original TOTAL_VALUE
        v_rows = [voo_rows[i] for i in indices]
        v_shares = [float(r[3]) for r in v_rows]
        v_prices = [float(r[2]) for r in v_rows]
        v_total_shares = sum(v_shares)
        v_wavg = sum(p * s for p, s in zip(v_prices, v_shares)) / v_total_shares
        v_total_val = sum(float(r[4]) for r in v_rows)
        new_voo.append([v_rows[0][0], "VOO", f"{v_wavg:.2f}", f"{round(v_total_shares, 5)}", f"{v_total_val}"])

        # Merge shadow QQQ — sum original TOTAL_VALUE
        q_rows = [qqq_rows[i] for i in indices]
        q_shares = [float(r[3]) for r in q_rows]
        q_prices = [float(r[2]) for r in q_rows]
        q_total_shares = sum(q_shares)
        q_wavg = sum(p * s for p, s in zip(q_prices, q_shares)) / q_total_shares
        q_total_val = sum(float(r[4]) for r in q_rows)
        new_qqq.append([q_rows[0][0], "QQQ", f"{q_wavg:.2f}", f"{round(q_total_shares, 5)}", f"{q_total_val}"])

        # Log
        before = "; ".join(f"{r[3]}@${r[2]}" for r in t_rows)
        log_entries.append(f"  {date} {ticker}: [{before}] → {merged_txn[3]}@${merged_txn[2]} (total ${round(total_value, 2)})")

    merged_count = len(txn_rows) - len(new_txn)
    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}{portfolio_id}: {len(txn_rows)} rows → {len(new_txn)} rows ({merged_count} rows merged)")

    if not log_entries:
        print("  No duplicates found.")
        return 0

    for entry in log_entries:
        print(entry)

    if DRY_RUN:
        return merged_count

    # Backup originals
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(root, f"backup_{timestamp}")
    os.makedirs(backup_dir, exist_ok=True)
    for name, path in paths.items():
        shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))
    print(f"  Backups saved to {backup_dir}/")

    # Write consolidated files
    write_csv(paths["transactions"], txn_header, new_txn)
    write_csv(paths["portfolio"], port_header, new_port)
    write_csv(paths["shadow_voo"], voo_header, new_voo)
    write_csv(paths["shadow_qqq"], qqq_header, new_qqq)

    # Write change log
    log_path = os.path.join(backup_dir, "changes.log")
    with open(log_path, "w") as f:
        f.write(f"Consolidation run: {timestamp}\n")
        f.write(f"Portfolio: {portfolio_id}\n")
        f.write(f"Rows before: {len(txn_rows)}, after: {len(new_txn)}, merged: {merged_count}\n\n")
        for entry in log_entries:
            f.write(entry + "\n")
    print(f"  Change log: {log_path}")

    return merged_count


if __name__ == "__main__":
    total = 0
    for name in sorted(os.listdir(PORTFOLIOS_DIR)):
        config = os.path.join(PORTFOLIOS_DIR, name, "config.json")
        if os.path.isfile(config):
            total += consolidate_portfolio(name)
    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}Total rows merged: {total}")
