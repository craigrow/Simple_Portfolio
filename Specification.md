# Simple Portfolio Tracker
### Technical Specification

### Overview
The goal of the simple portfolio tracker is to show how a set of portfolio transactions performed against the S&P 500 and the NASDAQ. To achieve this, we will create shadow portfolios for VOO and QQQ. For each portfolio investment, we will assume the same dollar amount was invested in VOO and QQQ. 

For example, we enter, into the main portfolio, the following transaction: [Buy, MSFT, 100 shares at $100]. The total value of that transaction is $10,000. Thus, we would record an investment of $10,000 in the VOO portfolio by finding the prices of VOO on the same date and then calculating the number of shares purchased. Assuming the price of VOO was $25 when the MSFT purhase was made, we would record a purchase of 400 VOO at $25. 

The portfolio will be stored in a table with the following headings.
1. DATE: The date of the transaction.
2. TICKER: The ticker of the security. We will track individual stocks, mutual funds, ETFs and crypto.
3. PURCHASE PRICE: The price paid per share.
4. SHARES PURCHASED: The quantity of shares. Note: it is expected most transactions, in both the main portfolio and the shadow portfolios will be in fractional shares. The application needs to handle fractional shares to five decimal points.
5. TOTAL VALUE: The total value of the transaction (PURCHASE PRICE * SHARES PURCHASED).

The two shadow portfolios, for QQQ and VOO, will have the same structure.

### Non-Functional Requirements
1. We want to get security prices from a free API such as yfinance.
2. In these early sprints, we can just run the web page on the local host. However, eventually this will be an app that needs to be available 24/7 from any device. We will want to host on a site that enables us to run for free or very low cost (<$5 per month).
3. Stability and robustness are critical. A full suite of unit tests and functional tests must be created as we implement each user story. We cannot move to a new user story until the full test suite is passing. This is critical to enable us to continue enhancing the applications while remaining highly available to users.

---

## Sprint 1

### User Story 1
As a user, I want to be able to enter transactions by editing a text file and then I want the app to show a web page displaying the full table of transactions in my portfolio and the shadow portfolios.

### Status: Complete

### Tech Stack
- **Python 3.12** with **Flask** for the web framework
- **yfinance** (v1.2.0+) for fetching historical security prices
- **pandas** for data handling
- **pytest** for testing

Flask was chosen for its simplicity and compatibility with low-cost hosting options (Render, Railway, fly.io).

### Architecture Decisions

1. **Separation of concerns**: Portfolio logic lives in `portfolio_engine.py`, separate from the Flask app (`app.py`). This makes it easy to swap frameworks, add a CLI, or reuse logic elsewhere.

2. **Input file vs. processed data**: The user edits `transactions.csv` to enter new transactions. Processed data is stored separately in the `data/` directory. This separation enables easy detection of new transactions by comparing row counts between the input file and the processed portfolio.

3. **Shadow portfolio persistence**: Shadow portfolios are calculated when new transactions are detected and persisted to CSV files, rather than recalculated on every page load. This avoids unnecessary API calls to yfinance.

4. **Sync on page load**: When the web page is loaded, the engine checks for new transactions and processes them before rendering. No background process is needed for Sprint 1.

5. **Closing price for shadow portfolios**: VOO and QQQ shadow entries use the closing price on the transaction date.

6. **Date format**: `YYYY-MM-DD` is the required format in `transactions.csv`.

7. **TOTAL_VALUE is calculated**: The user provides DATE, TICKER, PURCHASE_PRICE, and SHARES_PURCHASED. TOTAL_VALUE is computed by the engine.

### Project Structure
```
Simple_Portfolio/
├── transactions.csv           # User-edited input file (tracked in git)
├── data/
│   ├── .gitkeep
│   ├── portfolio.csv          # Processed main portfolio (git-ignored)
│   ├── shadow_voo.csv         # Persisted VOO shadow (git-ignored)
│   └── shadow_qqq.csv         # Persisted QQQ shadow (git-ignored)
├── app.py                     # Flask web app — single route, syncs + renders
├── portfolio_engine.py        # Core logic: read, sync, persist, load
├── requirements.txt           # flask, yfinance, pandas
├── templates/
│   └── index.html             # Displays all 3 tables with total invested
└── tests/
    ├── test_portfolio_engine.py  # 13 unit tests
    └── test_app.py               # 6 functional tests
```

### How to Run
```bash
pip install -r requirements.txt
python app.py
# Open http://127.0.0.1:5000
```

### How to Test
```bash
pip install pytest
python -m pytest tests/ -v
```

### Test Coverage (19 tests, all passing)
- CSV file creation and reading
- New transaction detection (empty, synced, appended)
- Sync: creates portfolio + shadow entries, no duplicates, incremental, empty no-op
- Fractional shares to 5 decimal places
- Shadow rows skipped when price data unavailable (e.g. non-trading day)
- Multiple transactions synced at once
- load_all returns three DataFrames
- Flask route: empty state, populated state, page title, total invested display, sync on load, all three tables present

### Known Limitations / Deferred to Future Sprints
- **Non-trading day validation**: Transactions on weekends/holidays will process to the main portfolio but won't generate shadow entries (no VOO/QQQ price available). Proper validation and error handling is deferred.
- **Sell transactions**: Only buys are supported. Sells will be needed in a future sprint.
- **No auto-refresh**: User must manually refresh the browser to see updates.
- **No duplicate detection**: If the same transaction is entered twice in `transactions.csv`, both will be processed.
- **Clearing data**: To re-sync from scratch, delete the three CSV files in `data/` and reload the page.
