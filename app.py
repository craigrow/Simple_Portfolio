from flask import Flask, render_template, request
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
                               shadow_qqq=[], columns=[], portfolio_value=0,
                               voo_value=0, qqq_value=0, portfolio_divs=0,
                               voo_divs=0, qqq_divs=0, portfolio_invested=0,
                               voo_invested=0, qqq_invested=0, history=[])
    if not portfolio_id or portfolio_id not in [p[0] for p in portfolios]:
        portfolio_id = portfolios[0][0]

    paths = portfolio_engine.get_paths(portfolio_id)
    portfolio_name = next(n for pid, n in portfolios if pid == portfolio_id)

    portfolio_engine.sync(paths)
    portfolio_engine.sync_splits(paths)
    portfolio_engine.sync_dividends(paths)
    port_df, shadow_voo_df, shadow_qqq_df = portfolio_engine.load_all(paths)
    splits_df = portfolio_engine._read_splits(paths)
    dividends_df = portfolio_engine._read_dividends(paths)

    prices_df = portfolio_engine.fetch_all_history(
        [port_df, shadow_voo_df, shadow_qqq_df], splits_df, dividends_df, paths
    )

    current_prices = {}
    if not prices_df.empty:
        for col in prices_df.columns:
            val = prices_df[col].iloc[-1]
            if pd.notna(val):
                current_prices[col] = round(float(val), 2)

    port_df, portfolio_value, portfolio_divs = portfolio_engine.enrich_portfolio(
        port_df, splits_df, dividends_df, current_prices)
    shadow_voo_df, voo_value, voo_divs = portfolio_engine.enrich_portfolio(
        shadow_voo_df, splits_df, dividends_df, current_prices)
    shadow_qqq_df, qqq_value, qqq_divs = portfolio_engine.enrich_portfolio(
        shadow_qqq_df, splits_df, dividends_df, current_prices)

    hist_main = portfolio_engine.get_historical_values(port_df, splits_df, dividends_df, prices_df)
    hist_voo = portfolio_engine.get_historical_values(shadow_voo_df, splits_df, dividends_df, prices_df)
    hist_qqq = portfolio_engine.get_historical_values(shadow_qqq_df, splits_df, dividends_df, prices_df)
    voo_by_date = {r["DATE"]: r["VALUE"] for r in hist_voo}
    qqq_by_date = {r["DATE"]: r["VALUE"] for r in hist_qqq}
    history = [
        {"DATE": r["DATE"], "MAIN": r["VALUE"],
         "VOO": voo_by_date.get(r["DATE"], 0.0),
         "QQQ": qqq_by_date.get(r["DATE"], 0.0)}
        for r in hist_main
    ]
    columns = portfolio_engine.COLUMNS + ["CURRENT_SHARES", "CURRENT_VALUE", "TOTAL_DIVIDENDS"]
    return render_template(
        "index.html",
        portfolios=portfolios,
        portfolio_id=portfolio_id,
        portfolio_name=portfolio_name,
        portfolio=port_df.to_dict("records") if not port_df.empty else [],
        shadow_voo=shadow_voo_df.to_dict("records") if not shadow_voo_df.empty else [],
        shadow_qqq=shadow_qqq_df.to_dict("records") if not shadow_qqq_df.empty else [],
        columns=columns,
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
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
