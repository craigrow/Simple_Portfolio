from flask import Flask, render_template, request, jsonify
import os
import pandas as pd
import portfolio_engine

app = Flask(__name__)


def _decision_rows(snapshot):
    rows = []
    for decision in snapshot["decisions"]:
        investment_irr = portfolio_engine.compute_irr(
            decision["original_investment"],
            decision["actual"]["total_return_value"],
            decision["buy_date"],
            decision["terminal_date"],
        )
        rows.append({
            "STATUS": decision["status"],
            "BUY_DATE": decision["buy_date"],
            "SELL_DATE": (
                decision["terminal_date"] if decision["status"] == "CLOSED" else None
            ),
            "TICKER": decision["ticker"],
            "ORIGINAL_INVESTMENT": decision["original_investment"],
            "ACTUAL_VALUE": decision["actual"]["total_return_value"],
            "ACTUAL_GAIN": decision["actual"]["gain_loss"],
            "IRR": investment_irr,
            "VOO_VALUE": decision["VOO"]["total_return_value"],
            "VS_VOO": decision["vs_voo"],
            "QQQ_VALUE": decision["QQQ"]["total_return_value"],
            "VS_QQQ": decision["vs_qqq"],
        })
    return sorted(rows, key=lambda row: row["BUY_DATE"], reverse=True)


def _holding_rows(snapshot):
    return [{
        "TICKER": holding["ticker"],
        "SHARES_OWNED": holding["shares"],
        "COST_BASIS": holding["cost_basis"],
        "CURRENT_VALUE": holding["current_value"],
        "DIVIDENDS": holding["distributions"],
        "GAIN_LOSS": holding["gain_loss"],
    } for holding in snapshot["actual"]["holdings"]]


def _refresh_all_portfolios(portfolios):
    results = {}
    failed = []
    incomplete = []
    for pid, _ in portfolios:
        result = portfolio_engine.refresh_data(portfolio_engine.get_paths(pid))
        results[pid] = result
        status = result.get("status")
        if status == "error":
            failed.append(pid)
        elif status != "ok":
            incomplete.append(pid)

    if failed:
        return {
            "status": "error",
            "message": "Refresh failed for: " + ", ".join(failed),
            "results": results,
        }
    if incomplete:
        return {
            "status": "incomplete",
            "message": "Refresh incomplete for: " + ", ".join(incomplete),
            "results": results,
        }
    return {"status": "ok", "message": "All portfolios refreshed", "results": results}


@app.route("/")
def index():
    portfolios = portfolio_engine.list_portfolios()
    portfolio_id = request.args.get("portfolio")
    if not portfolios:
        return render_template("index.html", portfolios=[], portfolio_id=None,
                               portfolio_name=None, portfolio=[], shadow_voo=[],
                               shadow_qqq=[], columns=[], shadow_columns=[], portfolio_value=0,
                               voo_value=0, qqq_value=0, portfolio_divs=0,
                               voo_divs=0, qqq_divs=0, portfolio_invested=0,
                               voo_invested=0, qqq_invested=0, history=[],
                               auto_refresh=False, accounting=None,
                               accounting_error=None, decisions=[])
    if not portfolio_id or portfolio_id not in [p[0] for p in portfolios]:
        portfolio_id = portfolios[0][0]

    paths = portfolio_engine.get_paths(portfolio_id)
    portfolio_name = next(n for pid, n in portfolios if pid == portfolio_id)

    # Sync any new transactions, then load cached data. If sync cannot repair
    # derived data, keep the dashboard available with the last readable cache.
    try:
        portfolio_engine.sync(paths)
        portfolio_engine.sync_manual_prices(paths)
    except Exception:
        pass
    port_df, shadow_voo_df, shadow_qqq_df = portfolio_engine.load_all(paths)
    splits_df = portfolio_engine._read_splits(paths)
    dividends_df = portfolio_engine._read_dividends(paths)

    prices_path = paths["price_history"]
    if os.path.exists(prices_path):
        prices_df = pd.read_csv(prices_path, index_col=0, parse_dates=True)
    else:
        prices_df = pd.DataFrame()

    current_prices = {}
    if not prices_df.empty:
        for col in prices_df.columns:
            last_idx = prices_df[col].last_valid_index()
            if last_idx is not None:
                current_prices[col] = round(float(prices_df[col].loc[last_idx]), 2)

    source_df = pd.read_csv(paths["transactions"])
    if not source_df.empty and "ACTION" in source_df.columns:
        accounting_error = None
        try:
            accounting = portfolio_engine.build_accounting_snapshot(paths, persist=True)
        except Exception as exc:
            accounting = portfolio_engine.get_cached_accounting_snapshot(paths)
            accounting_error = str(exc)
        if accounting is not None:
            history = portfolio_engine.get_cached_daily_values(paths)
            if not history and os.path.exists(prices_path):
                try:
                    history = portfolio_engine.compute_daily_values(paths)
                except Exception:
                    history = []
            portfolio_summary = _holding_rows(accounting)
            actual = accounting["actual"]
            voo = accounting["portfolio_benchmarks"]["VOO"]
            qqq = accounting["portfolio_benchmarks"]["QQQ"]
            portfolio_divs = sum(lot["distributions"] for lot in actual["lots"])
            voo_divs = sum(
                event["amount"] for event in voo["events"]
                if event["type"] == "DIVIDEND"
            )
            qqq_divs = sum(
                event["amount"] for event in qqq["events"]
                if event["type"] == "DIVIDEND"
            )
            market_today = portfolio_engine.get_market_comparison(
                actual["total_value"], voo["total_value"], qqq["total_value"], paths,
            )
            gainers, losers, pct_gainers, pct_losers = portfolio_engine.get_gainers_losers(
                portfolio_summary, paths
            )
            return render_template(
                "index.html",
                portfolios=portfolios,
                portfolio_id=portfolio_id,
                portfolio_name=portfolio_name,
                accounting=accounting,
                accounting_error=accounting_error,
                decisions=_decision_rows(accounting),
                portfolio_summary=portfolio_summary,
                history=history,
                market_today=market_today,
                gainers=gainers,
                losers=losers,
                pct_gainers=pct_gainers,
                pct_losers=pct_losers,
                last_updated=portfolio_engine.get_last_updated(paths),
                needs_refresh=portfolio_engine.needs_refresh(paths),
                auto_refresh=portfolio_engine.should_auto_refresh(paths),
                portfolio=[], shadow_voo=[], shadow_qqq=[], columns=[], shadow_columns=[],
                portfolio_value=actual["total_value"], voo_value=voo["total_value"],
                qqq_value=qqq["total_value"], portfolio_divs=portfolio_divs,
                voo_divs=voo_divs, qqq_divs=qqq_divs,
                portfolio_invested=actual["cumulative_contributions"],
                voo_invested=voo["cumulative_contributions"],
                qqq_invested=qqq["cumulative_contributions"],
            )
        # Never fall through to the legacy shadow calculation for an event
        # ledger. Without a last-known-good snapshot, show an explicit empty
        # state and the replay error until the data is corrected/refreshed.
        return render_template(
            "index.html",
            portfolios=portfolios, portfolio_id=portfolio_id,
            portfolio_name=portfolio_name, accounting=None,
            accounting_error=accounting_error, decisions=[],
            portfolio_summary=[], history=[], market_today=None,
            gainers=[], losers=[], pct_gainers=[], pct_losers=[],
            last_updated=portfolio_engine.get_last_updated(paths),
            needs_refresh=portfolio_engine.needs_refresh(paths),
            auto_refresh=portfolio_engine.should_auto_refresh(paths),
            portfolio=[], shadow_voo=[], shadow_qqq=[], columns=[], shadow_columns=[],
            portfolio_value=0, voo_value=0, qqq_value=0,
            portfolio_divs=0, voo_divs=0, qqq_divs=0,
            portfolio_invested=0, voo_invested=0, qqq_invested=0,
        )

    manual_divs = portfolio_engine.manual_dividends_by_ticker(paths)
    port_df, portfolio_value, portfolio_divs = portfolio_engine.enrich_portfolio(
        port_df, splits_df, dividends_df, current_prices, manual_dividends=manual_divs)
    shadow_voo_df, voo_value, voo_divs = portfolio_engine.enrich_portfolio(
        shadow_voo_df, splits_df, dividends_df, current_prices)
    shadow_qqq_df, qqq_value, qqq_divs = portfolio_engine.enrich_portfolio(
        shadow_qqq_df, splits_df, dividends_df, current_prices)

    # Chart from cached daily values (no computation on the fast path). If the
    # cache is empty but we have price history and holdings — e.g. a new
    # portfolio, or just after caches were cleared while prices are still fresh
    # (so no auto-refresh fires) — rebuild it once so the chart isn't blank.
    history = portfolio_engine.get_cached_daily_values(paths)
    if not history and not port_df.empty and os.path.exists(prices_path):
        try:
            history = portfolio_engine.compute_daily_values(paths)
        except Exception:
            history = []
    columns = portfolio_engine.COLUMNS + ["CURRENT_SHARES", "CURRENT_VALUE", "TOTAL_DIVIDENDS", "TOTAL_RETURN", "GAIN_LOSS", "VS_VOO", "VS_QQQ", "IRR"]
    shadow_columns = portfolio_engine.COLUMNS + ["CURRENT_SHARES", "CURRENT_VALUE", "TOTAL_DIVIDENDS", "TOTAL_RETURN", "GAIN_LOSS"]
    port_df = portfolio_engine.add_comparison_columns(port_df, shadow_voo_df, shadow_qqq_df)
    portfolio_summary = portfolio_engine.portfolio_summary(port_df)
    market_today = portfolio_engine.get_market_comparison(
        portfolio_value + portfolio_divs,
        voo_value + voo_divs,
        qqq_value + qqq_divs,
        paths,
    )
    gainers, losers, pct_gainers, pct_losers = portfolio_engine.get_gainers_losers(portfolio_summary, paths)
    return render_template(
        "index.html",
        portfolios=portfolios,
        portfolio_id=portfolio_id,
        portfolio_name=portfolio_name,
        portfolio=port_df.to_dict("records") if not port_df.empty else [],
        portfolio_summary=portfolio_summary,
        shadow_voo=shadow_voo_df.to_dict("records") if not shadow_voo_df.empty else [],
        shadow_qqq=shadow_qqq_df.to_dict("records") if not shadow_qqq_df.empty else [],
        columns=columns,
        shadow_columns=shadow_columns,
        portfolio_value=portfolio_value,
        voo_value=voo_value,
        qqq_value=qqq_value,
        portfolio_divs=portfolio_divs,
        voo_divs=voo_divs,
        qqq_divs=qqq_divs,
        portfolio_invested=port_df["TOTAL_VALUE"].sum() if not port_df.empty else 0.0,
        voo_invested=shadow_voo_df["TOTAL_VALUE"].sum() if not shadow_voo_df.empty else 0.0,
        qqq_invested=shadow_qqq_df["TOTAL_VALUE"].sum() if not shadow_qqq_df.empty else 0.0,
        history=history,
        market_today=market_today,
        gainers=gainers,
        losers=losers,
        pct_gainers=pct_gainers,
        pct_losers=pct_losers,
        last_updated=portfolio_engine.get_last_updated(paths),
        needs_refresh=portfolio_engine.needs_refresh(paths),
        auto_refresh=portfolio_engine.should_auto_refresh(paths),
        accounting=None,
        accounting_error=None,
        decisions=[],
    )


@app.route("/stats")
def stats():
    portfolios = portfolio_engine.list_portfolios()
    portfolio_id = request.args.get("portfolio")
    if portfolios and (not portfolio_id or portfolio_id not in [p[0] for p in portfolios]):
        portfolio_id = portfolios[0][0]

    portfolio_name = None
    stats_result = None
    stats_error = None
    if portfolios:
        portfolio_name = next(n for pid, n in portfolios if pid == portfolio_id)
        try:
            stats_result = portfolio_engine.get_baseball_stats(portfolio_engine.get_paths(portfolio_id), benchmark="VOO")
        except Exception as e:
            stats_error = str(e)

    return render_template(
        "stats.html",
        portfolios=portfolios,
        portfolio_id=portfolio_id,
        portfolio_name=portfolio_name,
        stats=stats_result,
        stats_error=stats_error,
    )


@app.route("/refresh")
def refresh():
    """Trigger data refresh — called by the Refresh button."""
    try:
        portfolio_id = request.args.get("portfolio")
        portfolios = portfolio_engine.list_portfolios()
        if not portfolios:
            return jsonify({"status": "ok", "message": "No portfolios"})
        force = request.args.get("force") == "1"
        if request.args.get("all") == "1":
            if force:
                for pid, _ in portfolios:
                    portfolio_engine.invalidate_all_caches(
                        portfolio_engine.get_paths(pid))
            return jsonify(_refresh_all_portfolios(portfolios))
        if not portfolio_id or portfolio_id not in [p[0] for p in portfolios]:
            portfolio_id = portfolios[0][0]
        paths = portfolio_engine.get_paths(portfolio_id)
        if force:
            portfolio_engine.invalidate_all_caches(paths)
        result = portfolio_engine.refresh_data(paths)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
