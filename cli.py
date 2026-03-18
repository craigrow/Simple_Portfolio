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
    parser = argparse.ArgumentParser(description="Simple Portfolio CLI")
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="Skip syncing transactions, splits, and dividends",
    )
    args = parser.parse_args()

    if not args.skip_sync:
        n = portfolio_engine.sync()
        if n:
            print(f"Synced {n} new transaction(s).")
        portfolio_engine.sync_splits()
        portfolio_engine.sync_dividends()

    portfolio, shadow_voo, shadow_qqq = portfolio_engine.load_all()
    portfolio, p_val, p_div = portfolio_engine.enrich_portfolio(portfolio)
    shadow_voo, v_val, v_div = portfolio_engine.enrich_portfolio(shadow_voo)
    shadow_qqq, q_val, q_div = portfolio_engine.enrich_portfolio(shadow_qqq)

    p_invested = portfolio["TOTAL_VALUE"].sum() if not portfolio.empty else 0.0
    v_invested = shadow_voo["TOTAL_VALUE"].sum() if not shadow_voo.empty else 0.0
    q_invested = shadow_qqq["TOTAL_VALUE"].sum() if not shadow_qqq.empty else 0.0

    _print_table("My Portfolio", portfolio, p_invested, p_val, p_div)
    _print_table("Shadow VOO", shadow_voo, v_invested, v_val, v_div)
    _print_table("Shadow QQQ", shadow_qqq, q_invested, q_val, q_div)


if __name__ == "__main__":
    main()
