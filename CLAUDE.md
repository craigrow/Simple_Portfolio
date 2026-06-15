# Simple Portfolio Tracker

## Quick Reference

```bash
# Run locally
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
flask run  # http://127.0.0.1:5000

# Run tests (all must pass before any merge)
python -m pytest tests/ -v
```

## Architecture

- **app.py** — Flask routes (`/`, `/refresh`)
- **portfolio_engine.py** — All business logic (sync, enrichment, price fetching, shadow portfolios)
- **templates/index.html** — Single-page UI with Chart.js
- **portfolios/<id>/** — Each portfolio is a self-contained folder with `config.json`, `transactions.csv`, and `data/`
- **init_data.py** — Seeds transaction data to Render's persistent disk on deploy

## Branching Model

```
feature/*  →  staging  →  main
  (dev)       (test)     (prod)
```

- All work happens in feature branches off `staging`
- Create a new branch for every new feature (`feature/<name>`)
- All tests must pass before merging to `staging` or `main`
- Never commit directly to `staging` or `main`
- **UAT locally first** — run the app and verify the feature works before merging to staging (deploying to Render introduces lag)
- **Main must be working and correct at all times.** Always push to staging first, verify on the staging URL, then promote known-good changes to main. Never push untested changes directly to main.

## Testing

98 tests across three files:
- `tests/test_portfolio_engine.py` — unit tests for business logic
- `tests/test_app.py` — functional tests for routes and templates
- `tests/test_init_data.py` — deploy initialization tests

- Every new feature must include comprehensive new tests (unit and functional)
- Augment existing tests as needed when behavior changes
- **Never promote code unless all tests are passing** — zero failures required before any merge
- Tests use temp directories to avoid polluting real data. Always run the full suite, not individual files.

## Conventions

- Commit messages: imperative mood, short first line (e.g., "Fix ZEC crypto ticker sync")
- No direct API calls in `app.py` — all data access goes through `portfolio_engine.py`
- Shadow portfolios (VOO, QQQ) mirror every transaction at the same dollar amount
- Price/split/dividend data cached in CSV files; staleness-gated refresh once per trading day
- Fractional shares handled to 5 decimal places

## Deployment

Hosted on Render with auto-deploy:
- **Production**: `main` branch → simple-portfolio-u41t.onrender.com
- **Staging**: `staging` branch → simple-portfolio-staging.onrender.com

Each environment has a separate persistent disk at `/data`.

## Key Gotchas

- macOS port 5000 conflicts with AirPlay Receiver — app may use port 5001 locally
- `data/` directories are git-ignored; only `transactions.csv` and `config.json` are tracked
- yfinance can be flaky — tests mock all external API calls
