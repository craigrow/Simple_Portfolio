from flask import Flask, render_template
import portfolio_engine

app = Flask(__name__)


@app.route("/")
def index():
    portfolio_engine.sync()
    portfolio_engine.sync_splits()
    portfolio_engine.sync_dividends()
    portfolio, shadow_voo, shadow_qqq = portfolio_engine.load_all()
    portfolio, portfolio_value, portfolio_divs = portfolio_engine.enrich_portfolio(portfolio)
    shadow_voo, voo_value, voo_divs = portfolio_engine.enrich_portfolio(shadow_voo)
    shadow_qqq, qqq_value, qqq_divs = portfolio_engine.enrich_portfolio(shadow_qqq)
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
    )


if __name__ == "__main__":
    app.run(debug=True)
