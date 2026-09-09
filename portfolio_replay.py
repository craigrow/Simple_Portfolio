"""Deterministic portfolio accounting for sells, cash, benchmarks, and XIRR.

This module implements the contracts in Sell_Transactions_Design.md.  It is
deliberately independent of Flask and yfinance: callers provide cached market
facts and receive one internally reconciled snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
import json
import math
import os
import tempfile

import pandas as pd


getcontext().prec = 34

MONEY = Decimal("0.01")
SHARE_TOLERANCE = Decimal("0.00000001")
BENCHMARKS = ("VOO", "QQQ")
LEGACY_COLUMNS = ("DATE", "TICKER", "PURCHASE_PRICE", "SHARES_PURCHASED")
LEDGER_COLUMNS = ("TRANSACTION_ID", "DATE", "ACTION", "TICKER", "PRICE", "SHARES", "LOT_ID")
SNAPSHOT_VERSION = 1


class ReplayError(ValueError):
    """Raised when source data cannot produce a valid atomic snapshot."""


def _decimal(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ReplayError(f"Invalid {field}: {value!r}") from None
    if not result.is_finite():
        raise ReplayError(f"Invalid {field}: {value!r}")
    return result


def _money(value):
    return _decimal(value, "money").quantize(MONEY, rounding=ROUND_HALF_UP)


def _date(value, field="date"):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ReplayError(f"Invalid {field}: {value!r}; expected YYYY-MM-DD") from None


def _number(value):
    """JSON-safe numeric output without changing posted cent values."""
    return float(value)


def normalize_transactions(transactions):
    """Validate new or legacy transactions and return deterministic events.

    Legacy buy-only rows receive stable in-memory IDs based on source row order.
    The migration command can later persist those IDs without changing values.
    """
    columns = set(transactions.columns)
    is_legacy = set(LEGACY_COLUMNS).issubset(columns) and "ACTION" not in columns
    if not is_legacy:
        missing = set(LEDGER_COLUMNS) - columns
        if missing:
            raise ReplayError("Transaction ledger missing columns: " + ", ".join(sorted(missing)))

    events = []
    seen_ids = set()
    for position, (_, row) in enumerate(transactions.iterrows(), start=1):
        if is_legacy:
            transaction_id = f"B{position:06d}"
            action = "BUY"
            price_value = row["PURCHASE_PRICE"]
            shares_value = row["SHARES_PURCHASED"]
            lot_id = ""
        else:
            transaction_id = str(row["TRANSACTION_ID"]).strip()
            action = str(row["ACTION"]).strip().upper()
            price_value = row["PRICE"]
            shares_value = row["SHARES"]
            lot_id = "" if pd.isna(row["LOT_ID"]) else str(row["LOT_ID"]).strip()

        if not transaction_id or transaction_id.lower() == "nan":
            raise ReplayError(f"Row {position}: TRANSACTION_ID is required")
        if transaction_id in seen_ids:
            raise ReplayError(f"Duplicate TRANSACTION_ID: {transaction_id}")
        seen_ids.add(transaction_id)
        if action not in {"BUY", "SELL"}:
            raise ReplayError(f"{transaction_id}: ACTION must be BUY or SELL")

        ticker = str(row["TICKER"]).strip()
        if not ticker or ticker.lower() == "nan":
            raise ReplayError(f"{transaction_id}: TICKER is required")
        price = _decimal(price_value, f"{transaction_id} PRICE")
        shares = _decimal(shares_value, f"{transaction_id} SHARES")
        if price <= 0 or shares <= 0:
            raise ReplayError(f"{transaction_id}: PRICE and SHARES must be positive")
        if action == "BUY" and lot_id:
            raise ReplayError(f"{transaction_id}: BUY must not have LOT_ID")
        if action == "SELL" and not lot_id:
            raise ReplayError(f"{transaction_id}: SELL requires LOT_ID")

        events.append({
            "transaction_id": transaction_id,
            "date": _date(row["DATE"], f"{transaction_id} DATE"),
            "action": action,
            "ticker": ticker,
            "price": price,
            "shares": shares,
            "lot_id": lot_id,
            "source_order": position,
            "amount": _money(price * shares),
            "legacy": is_legacy,
        })

    # Sells precede buys on a date. Source order is the tie-breaker.
    events.sort(key=lambda event: (
        event["date"], 0 if event["action"] == "SELL" else 1, event["source_order"]
    ))
    return events


class MarketBook:
    """Read-only normalized access to cached prices, splits, and dividends."""

    def __init__(self, prices, splits=None, dividends=None):
        self.prices = prices.copy() if prices is not None else pd.DataFrame()
        if not self.prices.empty:
            normalized = pd.to_datetime(self.prices.index).tz_localize(None).normalize()
            self.prices.index = normalized
            self.prices = self.prices[~self.prices.index.duplicated(keep="last")].sort_index()

        self.splits = []
        if splits is not None and not splits.empty:
            required = {"TICKER", "DATE", "RATIO"}
            if not required.issubset(splits.columns):
                raise ReplayError("Split data missing required columns")
            seen = set()
            for _, row in splits.iterrows():
                ticker = str(row["TICKER"]).strip()
                split_date = _date(row["DATE"], "split DATE")
                ratio = _decimal(row["RATIO"], "split RATIO")
                key = (ticker, split_date)
                if key in seen or ratio <= 0:
                    raise ReplayError(f"Invalid or duplicate split event: {ticker} {split_date}")
                seen.add(key)
                self.splits.append((split_date, ticker, ratio))
            self.splits.sort()

        self.dividends = []
        if dividends is not None and not dividends.empty:
            required = {"TICKER", "DATE", "AMOUNT"}
            if not required.issubset(dividends.columns):
                raise ReplayError("Dividend data missing required columns")
            seen = set()
            for _, row in dividends.iterrows():
                ticker = str(row["TICKER"]).strip()
                dividend_date = _date(row["DATE"], "dividend DATE")
                amount = _decimal(row["AMOUNT"], "dividend AMOUNT")
                key = (ticker, dividend_date)
                if key in seen or amount < 0:
                    raise ReplayError(f"Invalid or duplicate dividend event: {ticker} {dividend_date}")
                seen.add(key)
                if amount:
                    self.dividends.append((dividend_date, ticker, amount))
            self.dividends.sort()

    @property
    def latest_date(self):
        if self.prices.empty:
            return None
        return self.prices.index.max().date()

    def price(self, ticker, event_date, max_age_days=7, allow_stale=False):
        if ticker not in self.prices.columns:
            raise ReplayError(f"No cached price history for {ticker}")
        target = pd.Timestamp(event_date)
        series = pd.to_numeric(self.prices[ticker], errors="coerce").dropna()
        prior = series.loc[series.index <= target]
        if prior.empty:
            raise ReplayError(f"No settled {ticker} price on or before {event_date}")
        market_ts = prior.index[-1]
        age = (target.date() - market_ts.date()).days
        if not allow_stale and age > max_age_days:
            raise ReplayError(
                f"No settled {ticker} price within {max_age_days} days before {event_date}"
            )
        price = _decimal(prior.iloc[-1], f"{ticker} price")
        if price <= 0:
            raise ReplayError(f"Invalid cached price for {ticker} on {market_ts.date()}")
        return price, market_ts.date()

    def split_factor(self, ticker, buy_date, target_date):
        factor = Decimal("1")
        for split_date, split_ticker, ratio in self.splits:
            if split_ticker == ticker and buy_date < split_date <= target_date:
                factor *= ratio
        return factor

    def adjusted_shares(self, ticker, shares, buy_date, target_date):
        return shares * self.split_factor(ticker, buy_date, target_date)


def _xirr(cash_flows):
    """Return an exact-date annual rate as a decimal, or None when unavailable."""
    by_date = defaultdict(Decimal)
    for flow_date, amount in cash_flows:
        by_date[flow_date] += _decimal(amount, "XIRR cash flow")
    flows = sorted((flow_date, amount) for flow_date, amount in by_date.items() if amount)
    if not flows or not any(amount < 0 for _, amount in flows) or not any(amount > 0 for _, amount in flows):
        return None
    first_date = flows[0][0]
    if flows[-1][0] <= first_date:
        return None

    numeric = [(flow_date, float(amount)) for flow_date, amount in flows]

    def npv(rate):
        if rate <= -1:
            return math.inf
        total = 0.0
        for flow_date, amount in numeric:
            years = (flow_date - first_date).days / 365.0
            try:
                total += amount / ((1.0 + rate) ** years)
            except (OverflowError, ZeroDivisionError):
                return math.copysign(math.inf, amount)
        return total

    low = -0.999999999
    high = 1.0
    low_value = npv(low)
    high_value = npv(high)
    while math.isfinite(high_value) and low_value * high_value > 0 and high < 1e12:
        high *= 10.0
        high_value = npv(high)
    if not math.isfinite(low_value) or math.isnan(high_value) or low_value * high_value > 0:
        return None

    for _ in range(256):
        midpoint = (low + high) / 2.0
        value = npv(midpoint)
        if abs(value) < 1e-9 or high - low < 1e-12:
            return midpoint
        if low_value * value <= 0:
            high = midpoint
        else:
            low = midpoint
            low_value = value
    return (low + high) / 2.0


def _cash_event(events, event_id, event_date, event_type, amount, before, after, lot_id=None):
    events.append({
        "event_id": event_id,
        "date": event_date.isoformat(),
        "sequence": len(events) + 1,
        "type": event_type,
        "amount": _number(amount),
        "cash_before": _number(before),
        "cash_after": _number(after),
        "lot_id": lot_id,
    })


def _sum_money(values):
    return _money(sum(values, Decimal("0")))


def replay(transactions, market, manual_dividends=None, manual_tickers=None, valuation_date=None):
    """Build one reconciled snapshot from normalized source and market data."""
    events = normalize_transactions(transactions)
    if not events:
        return _empty_snapshot(valuation_date or market.latest_date)

    if valuation_date is None:
        candidates = [events[-1]["date"]]
        if market.latest_date:
            candidates.append(market.latest_date)
        valuation_date = max(candidates)
    elif not isinstance(valuation_date, date):
        valuation_date = _date(valuation_date, "valuation date")

    events = [event for event in events if event["date"] <= valuation_date]
    if not events:
        return _empty_snapshot(valuation_date)
    manual_tickers = set(manual_tickers or ())
    manual_rows = []
    if manual_dividends is not None and not manual_dividends.empty:
        required = {"DATE", "TICKER", "AMOUNT"}
        if not required.issubset(manual_dividends.columns):
            raise ReplayError("Manual dividend data missing required columns")
        seen_manual = set()
        for position, (_, row) in enumerate(manual_dividends.iterrows(), start=1):
            dividend_date = _date(row["DATE"], "manual dividend DATE")
            ticker = str(row["TICKER"]).strip()
            amount = _money(row["AMOUNT"])
            key = (dividend_date, ticker)
            if key in seen_manual or amount < 0:
                raise ReplayError(f"Invalid or duplicate manual dividend: {ticker} {dividend_date}")
            seen_manual.add(key)
            if dividend_date <= valuation_date and amount:
                manual_rows.append((dividend_date, ticker, amount, position))

    tx_by_date = defaultdict(list)
    for event in events:
        tx_by_date[event["date"]].append(event)
    market_divs_by_date = defaultdict(list)
    for dividend_date, ticker, amount in market.dividends:
        if dividend_date <= valuation_date:
            market_divs_by_date[dividend_date].append((ticker, amount))
    manual_by_date = defaultdict(list)
    for dividend_date, ticker, amount, position in manual_rows:
        manual_by_date[dividend_date].append((ticker, amount, position))

    event_dates = sorted(set(tx_by_date) | set(market_divs_by_date) | set(manual_by_date))
    actual_lots = {}
    actual_cash = Decimal("0")
    actual_cash_events = []
    contribution_flows = []
    cumulative_contributions = Decimal("0")

    benchmark_state = {
        benchmark: {"cash": Decimal("0"), "lots": [], "events": []}
        for benchmark in BENCHMARKS
    }

    for event_date in event_dates:
        # Market dividends are credited before sells and buys. Manual tickers use
        # their authoritative lump-sum source instead of provider distributions.
        for ticker, per_share in market_divs_by_date[event_date]:
            if ticker not in manual_tickers:
                for lot in actual_lots.values():
                    if lot["ticker"] != ticker or lot["status"] != "OPEN" or lot["buy_date"] >= event_date:
                        continue
                    shares = market.adjusted_shares(ticker, lot["shares"], lot["buy_date"], event_date)
                    amount = _money(shares * per_share)
                    if not amount:
                        continue
                    before = actual_cash
                    actual_cash += amount
                    lot["distributions"].append((event_date, amount))
                    _cash_event(
                        actual_cash_events,
                        f"DIV:{ticker}:{event_date}:{lot['lot_id']}", event_date,
                        "DIVIDEND", amount, before, actual_cash, lot["lot_id"],
                    )

            if ticker in BENCHMARKS:
                state = benchmark_state[ticker]
                eligible_shares = sum(
                    market.adjusted_shares(ticker, lot["shares"], lot["date"], event_date)
                    for lot in state["lots"] if lot["date"] < event_date
                )
                amount = _money(eligible_shares * per_share)
                if amount:
                    before = state["cash"]
                    state["cash"] += amount
                    _cash_event(
                        state["events"], f"DIV:{ticker}:{event_date}", event_date,
                        "DIVIDEND", amount, before, state["cash"],
                    )

        for ticker, total_amount, position in manual_by_date[event_date]:
            eligible = [
                lot for lot in actual_lots.values()
                if lot["ticker"] == ticker and lot["status"] == "OPEN" and lot["buy_date"] < event_date
            ]
            if not eligible:
                raise ReplayError(f"Manual dividend has no eligible open lot: {ticker} {event_date}")
            adjusted = [
                market.adjusted_shares(ticker, lot["shares"], lot["buy_date"], event_date)
                for lot in eligible
            ]
            total_shares = sum(adjusted, Decimal("0"))
            allocated = Decimal("0")
            for index, (lot, shares) in enumerate(zip(eligible, adjusted)):
                amount = total_amount - allocated if index == len(eligible) - 1 else _money(total_amount * shares / total_shares)
                allocated += amount
                lot["distributions"].append((event_date, amount))
            before = actual_cash
            actual_cash += total_amount
            _cash_event(
                actual_cash_events, f"MDIV:{position}:{ticker}:{event_date}", event_date,
                "DIVIDEND", total_amount, before, actual_cash,
            )

        for event in tx_by_date[event_date]:
            transaction_id = event["transaction_id"]
            if event["action"] == "SELL":
                lot = actual_lots.get(event["lot_id"])
                if lot is None:
                    raise ReplayError(f"{transaction_id}: unknown or not-yet-open LOT_ID {event['lot_id']}")
                if lot["status"] != "OPEN":
                    raise ReplayError(f"{transaction_id}: lot {event['lot_id']} is already closed")
                if lot["ticker"] != event["ticker"]:
                    raise ReplayError(f"{transaction_id}: ticker does not match lot {event['lot_id']}")
                adjusted = market.adjusted_shares(
                    lot["ticker"], lot["shares"], lot["buy_date"], event_date
                )
                if abs(adjusted - event["shares"]) > SHARE_TOLERANCE:
                    raise ReplayError(
                        f"{transaction_id}: complete sale requires {adjusted} shares for lot {event['lot_id']}"
                    )
                proceeds = event["amount"]
                before = actual_cash
                actual_cash += proceeds
                _cash_event(
                    actual_cash_events, transaction_id, event_date, "SALE_PROCEEDS",
                    proceeds, before, actual_cash, lot["lot_id"],
                )
                lot.update({
                    "status": "CLOSED",
                    "sell_transaction_id": transaction_id,
                    "sell_date": event_date,
                    "sell_price": event["price"],
                    "sold_shares": event["shares"],
                    "sale_proceeds": proceeds,
                })
                # Validate both decision terminal prices now. Continuous
                # portfolio benchmarks intentionally receive no event.
                for benchmark in BENCHMARKS:
                    market.price(benchmark, event_date)
                continue

            # BUY
            if transaction_id in actual_lots:
                raise ReplayError(f"Duplicate buy lot ID: {transaction_id}")
            purchase_amount = event["amount"]
            cash_used = min(actual_cash, purchase_amount)
            contribution = purchase_amount - cash_used
            if contribution:
                before = actual_cash
                actual_cash += contribution
                cumulative_contributions += contribution
                contribution_flows.append((event_date, -contribution))
                _cash_event(
                    actual_cash_events, f"{transaction_id}:CONTRIBUTION", event_date,
                    "EXTERNAL_CONTRIBUTION", contribution, before, actual_cash, transaction_id,
                )
            before = actual_cash
            actual_cash -= purchase_amount
            _cash_event(
                actual_cash_events, transaction_id, event_date, "PURCHASE",
                -purchase_amount, before, actual_cash, transaction_id,
            )
            if actual_cash < Decimal("0"):
                raise ReplayError(f"{transaction_id}: cash became negative")

            actual_lots[transaction_id] = {
                "lot_id": transaction_id,
                "ticker": event["ticker"],
                "buy_date": event_date,
                "buy_price": event["price"],
                "shares": event["shares"],
                "original_investment": purchase_amount,
                "status": "OPEN",
                "sell_transaction_id": None,
                "sell_date": None,
                "sell_price": None,
                "sold_shares": None,
                "sale_proceeds": None,
                "distributions": [],
            }

            for benchmark in BENCHMARKS:
                state = benchmark_state[benchmark]
                if contribution:
                    before = state["cash"]
                    state["cash"] += contribution
                    _cash_event(
                        state["events"], f"{transaction_id}:PORTFOLIO:{benchmark}:CONTRIBUTION",
                        event_date, "EXTERNAL_CONTRIBUTION", contribution,
                        before, state["cash"], transaction_id,
                    )
                invest_amount = state["cash"]
                if invest_amount:
                    price, price_date = market.price(benchmark, event_date)
                    shares = invest_amount / price
                    before = state["cash"]
                    state["cash"] = Decimal("0")
                    state["lots"].append({
                        "event_id": f"{transaction_id}:PORTFOLIO:{benchmark}:BUY",
                        "source_transaction_id": transaction_id,
                        "date": event_date,
                        "price_date": price_date,
                        "price": price,
                        "shares": shares,
                        "amount": invest_amount,
                    })
                    _cash_event(
                        state["events"], f"{transaction_id}:PORTFOLIO:{benchmark}:BUY",
                        event_date, "PURCHASE", -invest_amount, before, state["cash"], transaction_id,
                    )

    actual_open_value = Decimal("0")
    actual_lot_records = []
    holdings = defaultdict(lambda: {
        "shares": Decimal("0"), "cost_basis": Decimal("0"),
        "current_value": Decimal("0"), "distributions": Decimal("0"),
    })
    for lot in actual_lots.values():
        distributions = _sum_money(amount for _, amount in lot["distributions"])
        if lot["status"] == "OPEN":
            current_shares = market.adjusted_shares(
                lot["ticker"], lot["shares"], lot["buy_date"], valuation_date
            )
            price, price_date = market.price(lot["ticker"], valuation_date, allow_stale=True)
            security_value = _money(current_shares * price)
            actual_open_value += security_value
            holding = holdings[lot["ticker"]]
            holding["shares"] += current_shares
            holding["cost_basis"] += lot["original_investment"]
            holding["current_value"] += security_value
            holding["distributions"] += distributions
        else:
            current_shares = Decimal("0")
            price_date = lot["sell_date"]
            security_value = lot["sale_proceeds"]
        total_return_value = security_value + distributions
        actual_lot_records.append({
            "lot_id": lot["lot_id"],
            "ticker": lot["ticker"],
            "buy_date": lot["buy_date"].isoformat(),
            "buy_price": _number(lot["buy_price"]),
            "original_shares": _number(lot["shares"]),
            "original_investment": _number(lot["original_investment"]),
            "status": lot["status"],
            "current_shares": _number(current_shares),
            "sell_transaction_id": lot["sell_transaction_id"],
            "sell_date": lot["sell_date"].isoformat() if lot["sell_date"] else None,
            "sell_price": _number(lot["sell_price"]) if lot["sell_price"] else None,
            "sale_proceeds": _number(lot["sale_proceeds"]) if lot["sale_proceeds"] is not None else None,
            "distributions": _number(distributions),
            "security_value": _number(security_value),
            "total_return_value": _number(total_return_value),
            "gain_loss": _number(total_return_value - lot["original_investment"]),
            "price_date": price_date.isoformat(),
        })

    actual_value = _money(actual_open_value + actual_cash)
    actual_xirr = _xirr(contribution_flows + [(valuation_date, actual_value)])

    portfolio_benchmarks = {}
    for benchmark in BENCHMARKS:
        state = benchmark_state[benchmark]
        price, price_date = market.price(benchmark, valuation_date, allow_stale=True)
        current_shares = sum(
            market.adjusted_shares(benchmark, lot["shares"], lot["date"], valuation_date)
            for lot in state["lots"]
        )
        security_value = _money(current_shares * price)
        total_value = _money(security_value + state["cash"])
        portfolio_benchmarks[benchmark] = {
            "ticker": benchmark,
            "shares": _number(current_shares),
            "cash": _number(state["cash"]),
            "security_value": _number(security_value),
            "total_value": _number(total_value),
            "cumulative_contributions": _number(cumulative_contributions),
            "gain_loss": _number(total_value - cumulative_contributions),
            "xirr": _xirr(contribution_flows + [(valuation_date, total_value)]),
            "price": _number(price),
            "price_date": price_date.isoformat(),
            "lots": [
                {
                    **{key: value for key, value in lot.items() if key not in {"date", "price_date", "price", "shares", "amount"}},
                    "date": lot["date"].isoformat(),
                    "price_date": lot["price_date"].isoformat(),
                    "price": _number(lot["price"]),
                    "shares": _number(lot["shares"]),
                    "amount": _number(lot["amount"]),
                }
                for lot in state["lots"]
            ],
            "events": state["events"],
        }

    decisions = _build_decisions(actual_lots, market, valuation_date)
    by_lot = {row["lot_id"]: row for row in actual_lot_records}
    for decision in decisions:
        by_lot[decision["lot_id"]].update({
            "decision_xirr": decision["actual"]["xirr"],
            "vs_voo": decision["actual"]["total_return_value"] - decision["VOO"]["total_return_value"],
            "vs_qqq": decision["actual"]["total_return_value"] - decision["QQQ"]["total_return_value"],
        })

    holding_records = []
    for ticker, values in sorted(holdings.items()):
        holding_records.append({
            "ticker": ticker,
            "shares": _number(values["shares"]),
            "cost_basis": _number(values["cost_basis"]),
            "current_value": _number(values["current_value"]),
            "distributions": _number(values["distributions"]),
            "gain_loss": _number(values["current_value"] + values["distributions"] - values["cost_basis"]),
        })

    snapshot = {
        "version": SNAPSHOT_VERSION,
        "valuation_date": valuation_date.isoformat(),
        "actual": {
            "cash": _number(actual_cash),
            "security_value": _number(_money(actual_open_value)),
            "total_value": _number(actual_value),
            "cumulative_contributions": _number(cumulative_contributions),
            "gain_loss": _number(actual_value - cumulative_contributions),
            "xirr": actual_xirr,
            "holdings": holding_records,
            "lots": actual_lot_records,
            "cash_events": actual_cash_events,
        },
        "portfolio_benchmarks": portfolio_benchmarks,
        "decisions": decisions,
    }
    _validate_snapshot(snapshot)
    return snapshot


def _build_decisions(actual_lots, market, valuation_date):
    decisions = []
    for lot in sorted(actual_lots.values(), key=lambda item: (item["buy_date"], item["lot_id"])):
        terminal_date = lot["sell_date"] or valuation_date
        actual_distributions = list(lot["distributions"])
        if lot["status"] == "CLOSED":
            actual_security_value = lot["sale_proceeds"]
            actual_price_date = lot["sell_date"]
        else:
            actual_shares = market.adjusted_shares(
                lot["ticker"], lot["shares"], lot["buy_date"], terminal_date
            )
            price, actual_price_date = market.price(lot["ticker"], terminal_date, allow_stale=True)
            actual_security_value = _money(actual_shares * price)

        actual_distribution_total = _sum_money(amount for _, amount in actual_distributions)
        actual_flows = [(lot["buy_date"], -lot["original_investment"])]
        actual_flows.extend(actual_distributions)
        actual_flows.append((terminal_date, actual_security_value))
        actual_record = {
            "security_value": _number(actual_security_value),
            "distributions": _number(actual_distribution_total),
            "total_return_value": _number(actual_security_value + actual_distribution_total),
            "gain_loss": _number(actual_security_value + actual_distribution_total - lot["original_investment"]),
            "xirr": _xirr(actual_flows),
            "price_date": actual_price_date.isoformat(),
        }

        decision = {
            "lot_id": lot["lot_id"],
            "ticker": lot["ticker"],
            "status": lot["status"],
            "buy_date": lot["buy_date"].isoformat(),
            "terminal_date": terminal_date.isoformat(),
            "original_investment": _number(lot["original_investment"]),
            "actual": actual_record,
        }
        for benchmark in BENCHMARKS:
            start_price, start_price_date = market.price(benchmark, lot["buy_date"])
            starting_shares = lot["original_investment"] / start_price
            terminal_price, terminal_price_date = market.price(benchmark, terminal_date, allow_stale=terminal_date == valuation_date)
            terminal_shares = market.adjusted_shares(
                benchmark, starting_shares, lot["buy_date"], terminal_date
            )
            terminal_value = _money(terminal_shares * terminal_price)
            distributions = []
            for dividend_date, dividend_ticker, per_share in market.dividends:
                if dividend_ticker != benchmark or not (lot["buy_date"] < dividend_date <= terminal_date):
                    continue
                shares_on_date = market.adjusted_shares(
                    benchmark, starting_shares, lot["buy_date"], dividend_date
                )
                distributions.append((dividend_date, _money(shares_on_date * per_share)))
            distribution_total = _sum_money(amount for _, amount in distributions)
            flows = [(lot["buy_date"], -lot["original_investment"])]
            flows.extend(distributions)
            flows.append((terminal_date, terminal_value))
            total_return_value = terminal_value + distribution_total
            decision[benchmark] = {
                "start_price": _number(start_price),
                "start_price_date": start_price_date.isoformat(),
                "starting_shares": _number(starting_shares),
                "terminal_price": _number(terminal_price),
                "terminal_price_date": terminal_price_date.isoformat(),
                "security_value": _number(terminal_value),
                "distributions": _number(distribution_total),
                "total_return_value": _number(total_return_value),
                "gain_loss": _number(total_return_value - lot["original_investment"]),
                "xirr": _xirr(flows),
            }
        decision["vs_voo"] = decision["actual"]["total_return_value"] - decision["VOO"]["total_return_value"]
        decision["vs_qqq"] = decision["actual"]["total_return_value"] - decision["QQQ"]["total_return_value"]
        decisions.append(decision)
    return decisions


def _validate_snapshot(snapshot):
    actual = snapshot["actual"]
    for benchmark in BENCHMARKS:
        state = snapshot["portfolio_benchmarks"][benchmark]
        if abs(state["cumulative_contributions"] - actual["cumulative_contributions"]) > 0.005:
            raise ReplayError(f"{benchmark} contributions do not match actual contributions")
        expected_lead = actual["gain_loss"] - state["gain_loss"]
        value_lead = actual["total_value"] - state["total_value"]
        if abs(expected_lead - value_lead) > 0.011:
            raise ReplayError(f"{benchmark} value and gain/loss comparisons do not reconcile")
        if any(event["type"] == "SALE_PROCEEDS" for event in state["events"]):
            raise ReplayError(f"{benchmark} portfolio benchmark contains a sale")


def _empty_snapshot(valuation_date):
    valuation = valuation_date.isoformat() if isinstance(valuation_date, date) else None
    empty_benchmark = lambda ticker: {
        "ticker": ticker, "shares": 0.0, "cash": 0.0, "security_value": 0.0,
        "total_value": 0.0, "cumulative_contributions": 0.0, "gain_loss": 0.0,
        "xirr": None, "price": None, "price_date": None, "lots": [], "events": [],
    }
    return {
        "version": SNAPSHOT_VERSION,
        "valuation_date": valuation,
        "actual": {
            "cash": 0.0, "security_value": 0.0, "total_value": 0.0,
            "cumulative_contributions": 0.0, "gain_loss": 0.0, "xirr": None,
            "holdings": [], "lots": [], "cash_events": [],
        },
        "portfolio_benchmarks": {ticker: empty_benchmark(ticker) for ticker in BENCHMARKS},
        "decisions": [],
    }


def _load_inputs(paths):
    if not os.path.exists(paths["transactions"]):
        return None
    transactions = pd.read_csv(paths["transactions"])
    prices = pd.DataFrame()
    if os.path.exists(paths["price_history"]):
        prices = pd.read_csv(paths["price_history"], index_col=0, parse_dates=True)
    splits = pd.read_csv(paths["splits"]) if os.path.exists(paths["splits"]) else pd.DataFrame()
    dividends = pd.read_csv(paths["dividends"]) if os.path.exists(paths["dividends"]) else pd.DataFrame()
    manual_dividends = (
        pd.read_csv(paths["manual_dividends"])
        if os.path.exists(paths.get("manual_dividends", "")) else pd.DataFrame()
    )
    config = {}
    if os.path.exists(paths.get("config", "")):
        with open(paths["config"]) as config_file:
            config = json.load(config_file)
    manual_tickers = set(transactions["TICKER"].astype(str)) if config.get("manual_pricing") else set()
    return transactions, MarketBook(prices, splits, dividends), manual_dividends, manual_tickers


def build_snapshot(paths, valuation_date=None, persist=False):
    """Load a portfolio's cached facts and return the new accounting snapshot."""
    inputs = _load_inputs(paths)
    if inputs is None:
        return _empty_snapshot(valuation_date)
    transactions, market, manual_dividends, manual_tickers = inputs
    snapshot = replay(
        transactions,
        market,
        manual_dividends=manual_dividends,
        manual_tickers=manual_tickers,
        valuation_date=valuation_date,
    )
    if persist:
        snapshot_path = paths.get("accounting_snapshot") or os.path.join(paths["data_dir"], "accounting_snapshot.json")
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        descriptor, temp_path = tempfile.mkstemp(prefix="accounting_snapshot.", suffix=".tmp", dir=os.path.dirname(snapshot_path))
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(snapshot, output, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temp_path, snapshot_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
    return snapshot


def build_history(paths):
    """Return daily values derived from one fully reconciled replay.

    Replaying the entire ledger once per market day is correct but needlessly
    quadratic. The final snapshot already contains every dated cash event and
    lot boundary, so historical valuation can walk those immutable facts while
    applying each day's cached prices.
    """
    inputs = _load_inputs(paths)
    if inputs is None:
        return []
    transactions, market, manual_dividends, manual_tickers = inputs
    events = normalize_transactions(transactions)
    if not events or market.prices.empty:
        return []
    final_snapshot = replay(
        transactions,
        market,
        manual_dividends=manual_dividends,
        manual_tickers=manual_tickers,
    )
    first_date = events[0]["date"]
    prices = market.prices.ffill()

    def dated_cash(events_to_apply):
        return sorted(
            (
                _date(event["date"]),
                event["sequence"],
                _decimal(event["cash_after"], "historical cash"),
            )
            for event in events_to_apply
        )

    actual_cash_events = dated_cash(final_snapshot["actual"]["cash_events"])
    benchmark_cash_events = {
        benchmark: dated_cash(final_snapshot["portfolio_benchmarks"][benchmark]["events"])
        for benchmark in BENCHMARKS
    }
    contribution_events = sorted(
        (
            _date(event["date"]),
            event["sequence"],
            _decimal(event["amount"], "historical contribution"),
        )
        for event in final_snapshot["actual"]["cash_events"]
        if event["type"] == "EXTERNAL_CONTRIBUTION"
    )

    actual_lots = []
    for lot in final_snapshot["actual"]["lots"]:
        actual_lots.append({
            "ticker": lot["ticker"],
            "buy_date": _date(lot["buy_date"]),
            "sell_date": _date(lot["sell_date"]) if lot["sell_date"] else None,
            "shares": _decimal(lot["original_shares"], "historical shares"),
        })
    benchmark_lots = {
        benchmark: [
            {
                "date": _date(lot["date"]),
                "shares": _decimal(lot["shares"], "historical benchmark shares"),
            }
            for lot in final_snapshot["portfolio_benchmarks"][benchmark]["lots"]
        ]
        for benchmark in BENCHMARKS
    }

    actual_cash = Decimal("0")
    benchmark_cash = {benchmark: Decimal("0") for benchmark in BENCHMARKS}
    contributions = Decimal("0")
    actual_cash_index = 0
    benchmark_cash_index = {benchmark: 0 for benchmark in BENCHMARKS}
    contribution_index = 0
    provisional_dates = set()
    provisional_path = paths.get("provisional_prices")
    if provisional_path and os.path.exists(provisional_path):
        with open(provisional_path) as provisional_file:
            provisional_dates = {
                line.strip() for line in provisional_file
                if line.strip() and not line.startswith("#")
            }
    history = []
    for timestamp, price_row in prices.iterrows():
        valuation_date = timestamp.date()
        if valuation_date < first_date:
            continue

        while (
            actual_cash_index < len(actual_cash_events)
            and actual_cash_events[actual_cash_index][0] <= valuation_date
        ):
            actual_cash = actual_cash_events[actual_cash_index][2]
            actual_cash_index += 1
        daily_contributions = Decimal("0")
        while (
            contribution_index < len(contribution_events)
            and contribution_events[contribution_index][0] <= valuation_date
        ):
            contribution = contribution_events[contribution_index][2]
            contributions += contribution
            daily_contributions += contribution
            contribution_index += 1
        for benchmark in BENCHMARKS:
            cash_events = benchmark_cash_events[benchmark]
            index = benchmark_cash_index[benchmark]
            while index < len(cash_events) and cash_events[index][0] <= valuation_date:
                benchmark_cash[benchmark] = cash_events[index][2]
                index += 1
            benchmark_cash_index[benchmark] = index

        actual_security_value = Decimal("0")
        for lot in actual_lots:
            if lot["buy_date"] > valuation_date:
                continue
            if lot["sell_date"] is not None and lot["sell_date"] <= valuation_date:
                continue
            raw_price = price_row.get(lot["ticker"])
            if pd.isna(raw_price):
                raise ReplayError(
                    f"No historical {lot['ticker']} price on or before {valuation_date}"
                )
            shares = market.adjusted_shares(
                lot["ticker"], lot["shares"], lot["buy_date"], valuation_date
            )
            actual_security_value += _money(
                shares * _decimal(raw_price, "historical price")
            )

        values = {}
        benchmark_security_values = {}
        for benchmark in BENCHMARKS:
            raw_price = price_row.get(benchmark)
            if pd.isna(raw_price):
                raise ReplayError(
                    f"No historical {benchmark} price on or before {valuation_date}"
                )
            shares = sum(
                (
                    market.adjusted_shares(
                        benchmark, lot["shares"], lot["date"], valuation_date
                    )
                    for lot in benchmark_lots[benchmark]
                    if lot["date"] <= valuation_date
                ),
                Decimal("0"),
            )
            security_value = _money(
                shares * _decimal(raw_price, "historical benchmark price")
            )
            benchmark_security_values[benchmark] = security_value
            values[benchmark] = _money(security_value + benchmark_cash[benchmark])

        actual_security_value = _money(actual_security_value)
        actual_total = _money(actual_security_value + actual_cash)
        history.append({
            "DATE": valuation_date.isoformat(),
            "MAIN": _number(actual_total),
            "VOO": _number(values["VOO"]),
            "QQQ": _number(values["QQQ"]),
            "MAIN_CASH": _number(actual_cash),
            "MAIN_SECURITIES": _number(actual_security_value),
            "VOO_CASH": _number(benchmark_cash["VOO"]),
            "VOO_SECURITIES": _number(benchmark_security_values["VOO"]),
            "QQQ_CASH": _number(benchmark_cash["QQQ"]),
            "QQQ_SECURITIES": _number(benchmark_security_values["QQQ"]),
            "DAILY_CONTRIBUTIONS": _number(_money(daily_contributions)),
            "CONTRIBUTIONS": _number(_money(contributions)),
            "VS_VOO": _number(_money(actual_total - values["VOO"])),
            "VS_QQQ": _number(_money(actual_total - values["QQQ"])),
            "PRICE_STATUS": (
                "PROVISIONAL"
                if valuation_date.isoformat() in provisional_dates else "SETTLED"
            ),
        })
    if history and history[-1]["DATE"] == final_snapshot["valuation_date"]:
        expected = {
            "MAIN": final_snapshot["actual"]["total_value"],
            "VOO": final_snapshot["portfolio_benchmarks"]["VOO"]["total_value"],
            "QQQ": final_snapshot["portfolio_benchmarks"]["QQQ"]["total_value"],
            "CONTRIBUTIONS": final_snapshot["actual"]["cumulative_contributions"],
        }
        for field, value in expected.items():
            if abs(history[-1][field] - value) > 0.005:
                raise ReplayError(f"Historical {field} does not reconcile to current snapshot")
    return history
