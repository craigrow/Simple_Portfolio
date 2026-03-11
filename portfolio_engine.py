import os
import csv
import yfinance as yf
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TRANSACTIONS_FILE = os.path.join(os.path.dirname(__file__), "transactions.csv")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.csv")
SHADOW_VOO_FILE = os.path.join(DATA_DIR, "shadow_voo.csv")
SHADOW_QQQ_FILE = os.path.join(DATA_DIR, "shadow_qqq.csv")

COLUMNS = ["DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED", "TOTAL_VALUE"]


def _ensure_file(path):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(COLUMNS)


def read_csv(path):
    _ensure_file(path)
    return pd.read_csv(path)


def _get_closing_price(ticker, date_str):
    """Fetch closing price for ticker on a given date."""
    t = yf.Ticker(ticker)
    start = pd.Timestamp(date_str)
    end = start + pd.Timedelta(days=1)
    hist = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    if hist.empty:
        return None
    return round(float(hist["Close"].iloc[0]), 2)


def _build_shadow_row(date_str, shadow_ticker, total_value):
    price = _get_closing_price(shadow_ticker, date_str)
    if price is None:
        return None
    shares = round(total_value / price, 5)
    return [date_str, shadow_ticker, price, shares, round(price * shares, 2)]


def _append_rows(path, rows):
    _ensure_file(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


def get_new_transactions():
    """Return rows from transactions.csv not yet in portfolio.csv."""
    txn = pd.read_csv(TRANSACTIONS_FILE)
    portfolio = read_csv(PORTFOLIO_FILE)

    if portfolio.empty:
        return txn
    # Compare by row count — new transactions are appended at the end
    processed_count = len(portfolio)
    return txn.iloc[processed_count:]


def sync():
    """Process new transactions and update portfolio + shadow files."""
    new = get_new_transactions()
    if new.empty:
        return 0

    portfolio_rows = []
    voo_rows = []
    qqq_rows = []

    for _, row in new.iterrows():
        date_str = str(row["DATE"])
        ticker = str(row["TICKER"])
        price = float(row["PURCHASE_PRICE"])
        shares = round(float(row["SHARES_PURCHASED"]), 5)
        total = round(price * shares, 2)

        portfolio_rows.append([date_str, ticker, price, shares, total])

        voo_row = _build_shadow_row(date_str, "VOO", total)
        if voo_row:
            voo_rows.append(voo_row)

        qqq_row = _build_shadow_row(date_str, "QQQ", total)
        if qqq_row:
            qqq_rows.append(qqq_row)

    _append_rows(PORTFOLIO_FILE, portfolio_rows)
    _append_rows(SHADOW_VOO_FILE, voo_rows)
    _append_rows(SHADOW_QQQ_FILE, qqq_rows)

    return len(portfolio_rows)


def load_all():
    """Return all three portfolios as DataFrames."""
    return (
        read_csv(PORTFOLIO_FILE),
        read_csv(SHADOW_VOO_FILE),
        read_csv(SHADOW_QQQ_FILE),
    )
