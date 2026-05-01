from flask import Flask, render_template, request, jsonify
import os
import pandas as pd
import portfolio_engine

app = Flask(__name__)


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
                               voo_invested=0, qqq_invested=0, history=[])
    if not portfolio_id or portfolio_id not in [p[0] for p in portfolios]:
        portfolio_id = portfolios[0][0]

    paths = portfolio_engine.get_paths(portfolio_id)
    portfolio_name = next(n for pid, n in portfolios if pid == portfolio_id)

    # Sync any new transactions, then load cached data — no API calls
    portfolio_engine.sync(paths)
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

    port_df, portfolio_value, portfolio_divs = portfolio_engine.enrich_portfolio(
        port_df, splits_df, dividends_df, current_prices)
    shadow_voo_df, voo_value, voo_divs = portfolio_engine.enrich_portfolio(
        shadow_voo_df, splits_df, dividends_df, current_prices)
    shadow_qqq_df, qqq_value, qqq_divs = portfolio_engine.enrich_portfolio(
        shadow_qqq_df, splits_df, dividends_df, current_prices)

    # Chart from cached daily values (no computation on page load)
    history = portfolio_engine.get_cached_daily_values(paths)
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
    )


@app.route("/refresh")
def refresh():
    """Trigger data refresh — called by the Refresh button."""
    try:
        portfolio_id = request.args.get("portfolio")
        portfolios = portfolio_engine.list_portfolios()
        if not portfolios:
            return jsonify({"status": "ok", "message": "No portfolios"})
        if not portfolio_id or portfolio_id not in [p[0] for p in portfolios]:
            portfolio_id = portfolios[0][0]
        paths = portfolio_engine.get_paths(portfolio_id)
        result = portfolio_engine.refresh_data(paths)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
