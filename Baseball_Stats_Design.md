# Baseball Stats Feature Design

## Goal

Add a portfolio stats view that frames investing performance with baseball-style metrics. The first version should compare the selected portfolio against the S&P 500 benchmark (`VOO`). The design should also support adding a NASDAQ benchmark (`QQQ`) later without changing the metric definitions.

The feature should not clutter the main portfolio dashboard.

## Placement

Create a separate stats view rather than adding more cards to the main page.

Recommended route:

```text
/stats?portfolio=<portfolio_id>
```

The stats page should reuse the existing portfolio selector so the user can switch portfolios. The main dashboard remains the default first screen.

Future expansion can add a benchmark control:

```text
VOO | QQQ
```

or show both benchmark comparisons side by side.

## Metrics

### Batting Average

Batting average answers:

> What percentage of transactions are beating the S&P 500?

For each transaction, compare the main portfolio transaction against its matching benchmark shadow transaction.

Formula:

```text
winning_transactions / total_transactions
```

A transaction is a win when:

```text
main_total_return_including_dividends > benchmark_total_return_including_dividends
```

For the initial implementation, the benchmark is `VOO`.

Dividends count for both sides:

- Main stock, ETF, or crypto-linked holding dividends/distributions
- Benchmark ETF dividends/distributions

Ties should be excluded from the denominator:

```text
wins / (wins + losses)
```

### Slugging Percentage

Slugging percentage answers:

> How many bases does each transaction earn based on its return multiple?

For each transaction:

```text
multiple = (current_value + dividends) / invested_amount
```

Base scoring:

| Multiple | Bases |
|---|---:|
| `< 2x` | 0 |
| `>= 2x and < 3x` | 1 |
| `>= 3x and < 4x` | 2 |
| `>= 4x and < 5x` | 3 |
| `>= 5x and < 6x` | 4 |
| `>= 6x and < 7x` | 5 |
| Continue pattern | `floor(multiple) - 1` |

Formula:

```text
sum(transaction_bases) / total_transactions
```

Dividends count. Use total return, not price return only.

### Daily Win Percentage

Daily win percentage answers:

> On how many trading days did the portfolio beat the market that day?

Each day is a new opportunity to win or lose. This is not a cumulative return comparison.

Compare day-over-day percentage change from previous close:

```text
portfolio_daily_value_pct_change > benchmark_daily_value_pct_change
```

Formula:

```text
winning_days / (winning_days + losing_days)
```

Ties should be excluded from the denominator. With enough decimal precision, exact ties should be rare.

Dividends do not count for daily win percentage. This metric should use daily value change only, excluding dividends.

## Data Integrity Requirements

Every main portfolio transaction must have a matching benchmark shadow transaction for each benchmark used in the stats.

Missing shadow rows are a serious bug, not a normal condition to ignore. The stats implementation should detect this and surface a data integrity warning instead of silently excluding those transactions.

For the initial VOO implementation:

```text
len(portfolio.csv) == len(shadow_voo.csv)
```

For future QQQ support:

```text
len(portfolio.csv) == len(shadow_qqq.csv)
```

If a mismatch is found, the stats page should show a clear warning that identifies the affected portfolio and benchmark. The engine should expose enough detail to debug the issue, such as row counts and possibly the first missing transaction index.

## Safety and Failure Isolation

The baseball stats feature has a lower criticality than the main portfolio dashboard and a much lower criticality than portfolio data integrity. The design must preserve that risk hierarchy:

1. Portfolio data corruption is a disaster.
2. Main dashboard failure is a serious outage.
3. Baseball stats failure is undesirable but tolerable.

The implementation must therefore isolate baseball stats from both the main dashboard and write paths.

### Read-Only Requirement

Baseball stats calculations must be read-only.

Stats code may read:

- `portfolio.csv`
- `shadow_voo.csv`
- `shadow_qqq.csv`
- `daily_values.csv`
- `splits.csv`
- `dividends.csv`
- `price_history.csv`

Stats code must not write:

- `transactions.csv`
- `portfolio.csv`
- `shadow_voo.csv`
- `shadow_qqq.csv`
- `daily_values.csv`
- `splits.csv`
- `dividends.csv`
- `price_history.csv`
- `last_updated.txt`

Stats code must not call:

- `sync()`
- `refresh_data()`
- `update_prices()`
- `compute_daily_values()`
- yfinance or any other market-data API

The stats page should use cached portfolio data only. If cached data is missing or stale, the stats page may show an unavailable/incomplete message, but it should not refresh or repair data.

### Route Isolation

Baseball stats should live on `/stats`, not `/`.

The main dashboard route must not depend on `get_baseball_stats()` or any stats-specific computation. A stats bug should not be able to break the main dashboard page.

Stats errors should be caught inside the `/stats` route and rendered as a stats-specific warning. The route should avoid returning a generic server error for ordinary stats calculation failures such as missing cached chart data or benchmark integrity mismatches.

### Data Integrity Warnings Are Not Repairs

Missing benchmark shadow rows are serious and should be reported clearly, but the stats feature must not attempt automatic repair. Repair belongs in existing sync/refresh workflows or a future explicit data-integrity maintenance tool.

The stats page can say that stats are unavailable because portfolio data failed validation. It should not mutate files to make the validation pass.

## Engine Design

Add stats calculation functions to `portfolio_engine.py`. Keep the calculations independent from Flask rendering.

Recommended public function:

```python
def get_baseball_stats(paths, benchmark="VOO"):
    ...
```

Return shape:

```python
{
    "benchmark": "VOO",
    "batting_average": 0.42,
    "slugging_percentage": 0.85,
    "daily_win_percentage": 0.51,
    "counts": {
        "transaction_wins": 10,
        "transaction_losses": 14,
        "transaction_ties": 0,
        "daily_wins": 120,
        "daily_losses": 115,
        "daily_ties": 1
    },
    "integrity": {
        "ok": True,
        "message": None
    }
}
```

Benchmark mapping:

```python
BENCHMARK_SHADOW_PATHS = {
    "VOO": "shadow_voo",
    "QQQ": "shadow_qqq",
}
```

### Batting Average Implementation

Inputs:

- `portfolio.csv`
- Selected benchmark shadow CSV
- `splits.csv`
- `dividends.csv`
- latest prices from `price_history.csv`

Steps:

1. Load main and benchmark transaction rows.
2. Validate row counts match.
3. Enrich both sides with current value and dividends using existing enrichment logic.
4. Compare total return per aligned row:

```text
current_value + total_dividends - total_value
```

5. Count wins, losses, and ties.
6. Return `wins / (wins + losses)`.

### Slugging Percentage Implementation

Inputs are the same as batting average.

For each main transaction:

```text
total_return_value = current_value + total_dividends
multiple = total_return_value / total_value
bases = max(0, floor(multiple) - 1)
```

Return:

```text
sum(bases) / number_of_transactions
```

This stat is for the selected portfolio's picks. If the UI later shows benchmark slugging, run the same calculation against the benchmark shadow rows.

### Daily Win Percentage Implementation

Inputs:

- `daily_values.csv`

Expected columns currently include portfolio and benchmark daily values. Use the cached chart data so this calculation does not trigger market API calls.

Steps:

1. Load cached daily values.
2. Choose the benchmark daily value column for `VOO` or `QQQ`.
3. Compute day-over-day percentage change for portfolio value and benchmark value.
4. Compare each day:

```text
portfolio_pct_change > benchmark_pct_change  => win
portfolio_pct_change < benchmark_pct_change  => loss
equal or missing values                       => tie/excluded
```

5. Return `wins / (wins + losses)`.

Do not include dividends in this metric.

## UI Design

The `/stats` page should be compact and focused.

Initial layout:

- Header with portfolio selector
- Benchmark label: `vs S&P 500`
- Three primary metric cards:
  - Batting Average
  - Slugging Percentage
  - Daily Win Percentage
- Small count details below each metric
- Data integrity warning area, shown only when needed

Avoid adding these stats to the main dashboard by default.

## Test Plan

Add focused tests for:

1. Batting average counts wins and losses correctly against VOO.
2. Batting average excludes ties from the denominator.
3. Batting average includes dividends for both main and benchmark rows.
4. Slugging percentage assigns bases by return multiple.
5. Slugging percentage includes dividends.
6. Daily win percentage compares day-over-day percentage changes, not cumulative return.
7. Daily win percentage excludes dividends.
8. Daily win percentage excludes ties from the denominator.
9. Missing benchmark shadow rows are reported as a data integrity issue.
10. `/stats?portfolio=<id>` renders the selected portfolio stats without cluttering `/`.
11. `/` still renders if baseball stats calculation raises an exception.
12. `/stats` catches baseball stats calculation errors and renders a stats-specific warning.
13. Baseball stats functions do not write to portfolio, shadow, price, dividend, split, daily value, transaction, or last-updated files.
14. Baseball stats functions do not call sync, refresh, price update, daily-value recompute, or external market-data APIs.

## Future Work

- Add QQQ/NASDAQ comparison using the same stats API.
- Add transaction-level drill-down showing hits, outs, and base values.
- Add daily win/loss streaks.
- Add explanatory tooltips for non-obvious metric definitions.
