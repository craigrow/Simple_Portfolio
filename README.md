# Simple Portfolio Tracker

A Flask app that tracks investment portfolios against VOO (S&P 500) and QQQ (NASDAQ) benchmarks.

**Production:** https://simple-portfolio-u41t.onrender.com  
**Staging:** https://simple-portfolio-staging.onrender.com

## Local Setup

```bash
git clone https://github.com/craigrow/Simple_Portfolio.git
cd Simple_Portfolio
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
flask run
```

Open http://127.0.0.1:5000

## Branching Model

```
feature/xxx  →  staging  →  main
   (dev)        (test)     (prod)
```

| Branch | Purpose | Deploys to |
|--------|---------|------------|
| `main` | Production. Always stable. | simple-portfolio-u41t.onrender.com |
| `staging` | Integration & UAT. | simple-portfolio-staging.onrender.com |
| `feature/*` | Development work. | Local only |

### Rules

1. **All development happens in feature branches off `staging`.** No direct commits to `staging` or `main`.
2. **All new features must include unit tests AND functional tests.** No feature is complete without test coverage for both the business logic (`test_portfolio_engine.py`) and the route/template behavior (`test_app.py`).
3. **All tests must pass before merging to `staging`.** Run the full suite and confirm 0 failures:
   ```bash
   python -m pytest tests/ -v
   ```
4. **All tests must pass before merging `staging` → `main`.** No exceptions — a single failure blocks promotion to production.
5. Create feature branches from `staging`:
   ```bash
   git checkout staging && git pull
   git checkout -b feature/my-feature
   ```
6. Merge feature → `staging` (triggers staging deploy).
7. UAT on staging URL. Verify the feature works end-to-end.
8. Only after UAT passes AND all tests pass: merge `staging` → `main` (triggers production deploy).

## Running Tests

```bash
source venv/bin/activate
pip install pytest  # first time only
python -m pytest tests/ -v
```

87 tests covering:
- **Unit tests** (`tests/test_portfolio_engine.py`): portfolio summary, price updates, splits, dividends, sync, dedup logic
- **Functional tests** (`tests/test_app.py`): route responses, JSON API, template rendering, stale data handling

All tests must pass before any merge. No exceptions.

## Project Structure

```
Simple_Portfolio/
├── app.py                  # Flask routes (/, /refresh)
├── portfolio_engine.py     # All business logic
├── templates/
│   └── index.html          # Single-page UI with Portfolio View
├── portfolios/
│   ├── foolish_portfolio/  # Motley Fool picks
│   │   ├── transactions.csv
│   │   ├── config.json
│   │   └── data/           # Generated: portfolio.csv, shadows, price_history, splits, dividends
│   └── tqqq_portfolio/
├── tests/
│   ├── test_portfolio_engine.py
│   └── test_app.py
├── init_data.py            # Copies transactions to persistent disk on deploy
├── Specification.md        # Full technical spec and user stories
└── requirements.txt
```

## Key Concepts

- **Shadow portfolios**: For every transaction, the app calculates what the same dollar amount would have bought in VOO and QQQ on the same date.
- **Price caching**: Prices are cached in `data/price_history.csv`. The Refresh button fetches only new data (delta fetch).
- **Persistent disk**: On Render, portfolio data lives on `/data` (persistent disk). `init_data.py` seeds `transactions.csv` from the repo on each deploy.

## Adding Transactions

Edit `portfolios/<portfolio>/transactions.csv` and add a row:

```
DATE,TICKER,PURCHASE_PRICE,SHARES_PURCHASED
2026-04-10,AAPL,175.50,5.0
```

Commit, push, and the app will process it on next page load.
