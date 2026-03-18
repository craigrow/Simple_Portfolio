#!/usr/bin/env python3
"""CLI for Simple Portfolio — mirrors the web app using portfolio_engine."""

import argparse
import portfolio_engine


def _fmt_money(val):
    return f"${val:,.2f}"


def _print_table(title, df, total_invested, current_value, dividends):
    header = (
        f"{title} | Total Invested {_fmt_money(total_invested)} | "
        f"Current Value {_fmt_money(current_value)} | "
        f"Dividends {_fmt_money(dividends)}"
    )
    print(f"\n{'=' * len(header)}")
    print(header)
    print("=" * len(header))
    if df.empty:
        print("  (no data)")
    else:
        print(df.to_string(index=False))
    print()


def main():
    portfolios = portfolio_engine.list_portfolios()
    parser = argparse.ArgumentParser(description="Simple Portfolio CLI")
    parser.add_argument(
        "--portfolio", "-p", default=None,
        help="Portfolio ID (folder name). Available: " + ", ".join(p[0] for p in portfolios),
    )
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="Skip syncing transactions, splits, and dividends",
    )
    args = parser.parse_args()

    if not portfolios:
        print("No portfolios found in portfolios/ directory.")
        return

    portfolio_id = args.portfolio or portfolios[0][0]
    if portfolio_id not in [p[0] for p in portfolios]:
        print(f"Unknown portfolio '{portfolio_id}'. Available: {', '.join(p[0] for p in portfolios)}")
        return

    paths = portfolio_engine.get_paths(portfolio_id)
    name = next(n for pid, n in portfolios if pid == portfolio_id)
    print(f"\n>>> {name} <<<")

    if not args.skip_sync:
        n = portfolio_engine.sync(paths)
        if n:
            print(f"Synced {n} new transaction(s).")
        portfolio_engine.sync_splits(paths)
        portfolio_engine.sync_dividends(paths)

    port, shadow_voo, shadow_qqq = portfolio_engine.load_all(paths)
    port, p_val, p_div = portfolio_engine.enrich_portfolio(port, paths=paths)
    shadow_voo, v_val, v_div = portfolio_engine.enrich_portfolio(shadow_voo, paths=paths)
    shadow_qqq, q_val, q_div = portfolio_engine.enrich_portfolio(shadow_qqq, paths=paths)

    p_invested = port["TOTAL_VALUE"].sum() if not port.empty else 0.0
    v_invested = shadow_voo["TOTAL_VALUE"].sum() if not shadow_voo.empty else 0.0
    q_invested = shadow_qqq["TOTAL_VALUE"].sum() if not shadow_qqq.empty else 0.0

    _print_table("My Portfolio", port, p_invested, p_val, p_div)
    _print_table("Shadow VOO", shadow_voo, v_invested, v_val, v_div)
    _print_table("Shadow QQQ", shadow_qqq, q_invested, q_val, q_div)


if __name__ == "__main__":
    main()
