from flask import Flask, render_template
import pandas as pd
import portfolio_engine

app = Flask(__name__)


@app.route("/")
def index():
    portfolio_engine.sync()
    portfolio_engine.sync_splits()
    portfolio_engine.sync_dividends()
    portfolio, shadow_voo, shadow_qqq = portfolio_engine.load_all()
    splits_df = portfolio_engine._read_splits()
    dividends_df = portfolio_engine._read_dividends()

    # Single download for all tickers across all portfolios
    prices_df = portfolio_engine.fetch_all_history(
        [portfolio, shadow_voo, shadow_qqq], splits_df, dividends_df
    )

    # Extract current prices from last row of historical data
    current_prices = {}
    if not prices_df.empty:
        for col in prices_df.columns:
            val = prices_df[col].iloc[-1]
            if pd.notna(val):
                current_prices[col] = round(float(val), 2)

    portfolio, portfolio_value, portfolio_divs = portfolio_engine.enrich_portfolio(
        portfolio, splits_df, dividends_df, current_prices)
    shadow_voo, voo_value, voo_divs = portfolio_engine.enrich_portfolio(
        shadow_voo, splits_df, dividends_df, current_prices)
    shadow_qqq, qqq_value, qqq_divs = portfolio_engine.enrich_portfolio(
        shadow_qqq, splits_df, dividends_df, current_prices)

    hist_main = portfolio_engine.get_historical_values(portfolio, splits_df, dividends_df, prices_df)
    hist_voo = portfolio_engine.get_historical_values(shadow_voo, splits_df, dividends_df, prices_df)
    hist_qqq = portfolio_engine.get_historical_values(shadow_qqq, splits_df, dividends_df, prices_df)
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
        portfolio=portfolio.to_dict("records") if not portfolio.empty else [],
        shadow_voo=shadow_voo.to_dict("records") if not shadow_voo.empty else [],
        shadow_qqq=shadow_qqq.to_dict("records") if not shadow_qqq.empty else [],
        columns=columns,
        portfolio_value=portfolio_value,
        voo_value=voo_value,
        qqq_value=qqq_value,
        portfolio_divs=portfolio_divs,
        voo_divs=voo_divs,
        qqq_divs=qqq_divs,
        portfolio_invested=portfolio["TOTAL_VALUE"].sum() if not portfolio.empty else 0.0,
        voo_invested=shadow_voo["TOTAL_VALUE"].sum() if not shadow_voo.empty else 0.0,
        qqq_invested=shadow_qqq["TOTAL_VALUE"].sum() if not shadow_qqq.empty else 0.0,
        history=history,
    )


if __name__ == "__main__":
    app.run(debug=True)
