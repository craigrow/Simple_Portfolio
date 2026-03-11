from flask import Flask, render_template
import portfolio_engine

app = Flask(__name__)


@app.route("/")
def index():
    portfolio_engine.sync()
    portfolio, shadow_voo, shadow_qqq = portfolio_engine.load_all()
    return render_template(
        "index.html",
        portfolio=portfolio.to_dict("records"),
        shadow_voo=shadow_voo.to_dict("records"),
        shadow_qqq=shadow_qqq.to_dict("records"),
        columns=portfolio_engine.COLUMNS,
    )


if __name__ == "__main__":
    app.run(debug=True)
