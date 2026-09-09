# Sell Transactions, Cash Accounting, Benchmark Views, and XIRR

## Status and authority

This is the implementation design for sell transactions, cash accounting, portfolio-level benchmark comparisons, per-decision benchmark comparisons, and XIRR in Simple Portfolio.

Implementation status as of September 9, 2026: the local feature branch implements and tests this design. All four checked-in source ledgers have been migrated to stable IDs and the `BUY`/`SELL` schema with their original facts preserved. Local real-data replay and UI UAT pass. Staging deployment, staging reconciliation, and production promotion remain rollout work.

This document supersedes every earlier proposal that:

- sold a portfolio-level VOO or QQQ position because an actual holding was sold;
- added synthetic catch-up cash to a benchmark portfolio;
- subtracted a permanent-loss adjustment from benchmark value;
- removed recovered principal from reported invested capital;
- maintained special `realized-gain cash` or `realized-loss basis` balances; or
- aggregated equal-dollar decision benchmarks to produce a portfolio benchmark.

Legacy generated shadow files remain on disk for rollback and reconciliation, but the event-ledger application path does not read them as portfolio benchmarks.

## Product questions and views

The application answers two different benchmark questions with separate derived views.

### Portfolio-level benchmark

> If every dollar of outside capital contributed to the actual portfolio had instead been contributed to VOO or QQQ on the same date, how much more or less money would the user have today?

The actual portfolio and both continuous portfolio benchmarks receive identical external contributions on identical dates. Actual sales do not cause portfolio-benchmark sales.

### Decision-level benchmark

> For this particular purchase, how did the selected security perform versus an equal-dollar investment in VOO or QQQ over the same holding period?

Every actual buy creates notional VOO and QQQ decision benchmarks. They are analytical records only and never contribute assets, cash, or value to a portfolio-level benchmark.

### Rate of return

> What annualized, money-weighted return did a portfolio or individual decision produce?

XIRR is calculated from dated cash flows. It is a metric applied to the actual portfolio, portfolio benchmarks, and decisions; it is not a third portfolio view.

## Terminology

| Term | Definition |
|---|---|
| Actual portfolio | The user's entered buys and sells, derived cash, open holdings, and inferred external contributions. |
| Portfolio benchmark | A continuous VOO or QQQ counterfactual receiving exactly the actual portfolio's external contributions on the same dates. |
| Decision benchmark | A notional equal-dollar VOO or QQQ investment paired with one actual buy solely for transaction analysis. |
| External contribution | New outside capital inferred when an actual buy cannot be fully funded from actual cash. |
| Internal cash flow | A retained dividend, sale, or purchase funded from cash. It is not an external portfolio-XIRR flow. |
| Valuation date | The settled market date through which current values are calculated. |

Legacy files may retain `shadow` in their names during migration. New domain code and UI copy must distinguish `portfolio benchmark` from `decision benchmark`.

## Governing invariants

1. The actual, VOO, and QQQ portfolios receive identical external contributions, in identical amounts, on identical dates.
2. No external withdrawals are supported in version one.
3. Actual cash starts at zero.
4. Actual dividends and sale proceeds enter actual cash.
5. An actual buy consumes actual cash first. Only its shortfall is new outside capital.
6. Each portfolio benchmark receives that shortfall, not the full actual purchase amount.
7. A portfolio benchmark never sells because an actual holding was sold. With no withdrawals, index principal stays invested continuously.
8. Portfolio-benchmark dividends enter that benchmark's cash and are reinvested at the next actual `BUY` event.
9. Every actual buy creates equal-dollar VOO and QQQ decision benchmarks, including a buy funded entirely by recycled cash.
10. Decision benchmarks never affect portfolio cash, holdings, contributions, value, charts, or portfolio XIRR.
11. Version one supports complete actual-lot sales only.
12. A closed actual decision and its decision benchmarks freeze at the actual sale date.
13. Source transactions and market data deterministically reproduce all derived state.
14. Invalid input or unavailable required data fails visibly and atomically; the previous valid generation remains active.

## Source transaction ledger

### Schema

Replace the current buy-only schema with:

```csv
TRANSACTION_ID,DATE,ACTION,TICKER,PRICE,SHARES,LOT_ID
B000001,2026-08-17,BUY,XYZ,10.00,100,
S000001,2027-02-15,SELL,XYZ,15.00,100,B000001
```

| Column | Required | Contract |
|---|---|---|
| `TRANSACTION_ID` | Always | Stable unique event ID within the portfolio. Never reused or changed after commit. |
| `DATE` | Always | ISO `YYYY-MM-DD` event date. |
| `ACTION` | Always | Exactly `BUY` or `SELL`. |
| `TICKER` | Always | Actual security ticker or configured manual-fund name. |
| `PRICE` | Always | Positive execution price per share or unit. Actual values use this entered price. |
| `SHARES` | Always | Original shares for a buy; complete split-adjusted shares sold for a sell. |
| `LOT_ID` | Sell only | `TRANSACTION_ID` of the actual buy being closed. Blank for buys. |

### Identity and precision

- Buy IDs use `B` plus a zero-padded sequence; sell IDs use `S` plus a zero-padded sequence.
- The actual lot ID is its buy transaction ID.
- Decision IDs derive from it, for example `B000001:DECISION:VOO`.
- Portfolio-benchmark event IDs derive from their triggering event.
- Matching uses IDs, never CSV row positions.
- Use decimal arithmetic, not binary floating point, for prices, shares, cash, and contributions.
- Preserve entered actual share precision and at least ten decimal places for derived benchmark shares.
- Post cash, contributions, proceeds, dividends, and displayed values to cents with one documented half-up rule.
- Preserve unrounded intermediates so replay is stable and rounding cannot create cash.
- `ORIGINAL_INVESTMENT` is the posted `PRICE × SHARES` amount and is authoritative for both decision benchmarks.

## Deterministic replay

Derived state is rebuilt from the complete source ledger and required market data. Append-only positional synchronization is not sufficient.

### Sort and same-day order

1. Validate the complete source schema and stable IDs.
2. Sort dates ascending.
3. Apply eligible dividend and split events before source transactions on that date.
4. Process every `SELL` before every `BUY`, regardless of CSV row order.
5. Preserve source order among same-day sells and among same-day buys.
6. Reject same-day round trips; a sell cannot reference a buy processed later that day.
7. Resolve every required VOO and QQQ benchmark price before committing the generation.

### Benchmark price-date convention

- Cache unadjusted closing prices (`auto_adjust=False`). Splits and dividends are posted separately, so adjusted closes would double-count them. Persist the price-policy version and fully rebuild provider-priced columns when it changes.
- On a benchmark trading date, use that date's settled closing price.
- If the actual transaction date is a weekend or benchmark-market holiday, use the most recent prior settled close, matching existing application behavior.
- Store both the actual event date and the market date from which the benchmark price came.
- Never use a future close, an intraday provisional price, or an unlimited stale forward-fill.
- The prior-close lookup may span at most seven calendar days. If no settled price is available in that window, fail replay.
- Portfolio-XIRR contributions retain the actual contribution date even when the benchmark execution price uses the prior-close proxy.

### Actual cash replay

Cash begins at `$0.00`.

```text
DIVIDEND:
    cash += dividend received

SELL:
    sale proceeds = posted sell price × complete adjusted shares
    cash += sale proceeds

BUY:
    purchase amount = posted buy price × shares
    cash used = min(cash, purchase amount)
    external contribution = purchase amount - cash used
    cash += external contribution - purchase amount
```

Cash must be nonnegative after every event, subject only to a one-cent posting tolerance. A contribution is inferred only at a buy and only for its exact shortfall.

### Applying contributions to portfolio benchmarks

Every inferred contribution is copied unchanged to both portfolio benchmarks on the same date:

```text
actual contribution(date)
  = VOO portfolio contribution(date)
  = QQQ portfolio contribution(date)
```

At each actual `BUY`, for each portfolio benchmark independently:

1. Credit the matching contribution to benchmark cash.
2. Include any benchmark dividends already waiting in cash.
3. Invest all available benchmark cash in the index at that date's closing price.
4. Leave only a documented sub-cent rounding remainder.

If the actual contribution is zero but benchmark dividend cash exists, reinvest the dividend cash. If both are zero, create no portfolio-benchmark trade.

### Actual sell processing

For every `SELL`:

1. Resolve `LOT_ID` to exactly one actual buy.
2. Confirm the lot is open and the tickers match.
3. Apply splits strictly after the buy and on or before the sale date.
4. Confirm sell shares equal the complete split-adjusted open shares within the decimal tolerance.
5. Calculate proceeds from the entered execution price and credit actual cash.
6. Freeze the actual lot's sale facts and close it.
7. Resolve VOO and QQQ closes on the sale date.
8. Freeze the two decision benchmarks as of that date.
9. Do not sell, resize, rebase, or mutate either continuous portfolio benchmark.

Users enter only the actual sell. Benchmark non-action and decision terminal snapshots are derived.

## Dividends and splits

### Cash treatment

- Retained dividends are internal portfolio income, not external portfolio-XIRR flows.
- Actual dividends enter actual cash.
- Portfolio-benchmark dividends enter that benchmark's independent cash account.
- Cash earns no interest in version one.
- Dividend cash remains cash until the next actual `BUY`, when each account applies its own cash rules.
- Dividend cash remaining on the valuation date is included in portfolio value.

### Eligibility convention

Version one treats the date stored in dividend data as both entitlement and cash-credit date. An actual or decision lot receives a dividend when:

```text
buy date < dividend event date <= sell date
```

for a closed lot, or through the valuation date for an open lot. A same-day buyer does not receive that day's dividend; a lot sold that day does.

If the provider supplies ex-dividend rather than payment dates, v1 uses that date as the cash-credit proxy. Changing date semantics later is a versioned accounting-policy change requiring full replay.

### Calculation rules

- For market-priced actual holdings, multiply the per-share dividend by shares owned on the event date, including effective splits. Credit it once to actual cash and attribute it once to the originating decision.
- `dividends_received.csv` remains authoritative for manual holdings. Allocate a ticker dividend among eligible actual lots in proportion to split-adjusted shares, credit the total once to cash, and retain allocations for decision metrics.
- Calculate portfolio-benchmark dividends from all shares in that continuous benchmark. Never derive them by summing decision-benchmark dividends.
- Actual open-lot splits run through valuation; closed-lot splits stop at sale.
- Portfolio-benchmark splits apply continuously.
- Decision-benchmark splits stop at the actual sale date or continue through valuation while open.
- Post-terminal events never change a frozen decision.

## Two benchmark systems

### Continuous portfolio benchmarks

Each portfolio benchmark contains index shares, dividend cash, cumulative external contributions identical to the actual portfolio's, and dated contribution/reinvestment events. An actual sale creates no event in it.

```text
actual value = actual cash + open-security market value
VOO value = VOO cash + VOO market value
QQQ value = QQQ cash + QQQ market value
```

Because contributions are identical:

```text
actual lead versus VOO = actual value - VOO value
actual lead versus QQQ = actual value - QQQ value

portfolio gain/loss = portfolio value - cumulative contributions

actual value - VOO value
  = actual gain/loss - VOO gain/loss
```

### Equal-dollar decision benchmarks

Every actual buy creates one VOO and one QQQ decision benchmark:

```text
decision investment = actual buy price × actual buy shares
benchmark starting shares = decision investment / benchmark close on buy date
```

They:

- exist even when the actual buy was funded entirely by cash;
- start with exactly the actual decision amount;
- receive independently calculated benchmark distributions;
- use the actual buy date as their start;
- use the actual sale date as terminal when closed, otherwise the valuation date;
- freeze when the actual lot closes; and
- never affect a portfolio benchmark or cash account.

For open decisions:

```text
decision total-return value
  = current security value + distributions attributed to the decision
```

For closed decisions:

```text
actual total-return value
  = sale proceeds + actual distributions through sale

benchmark total-return value
  = benchmark security value on sale date
  + benchmark distributions through sale
```

Because original investments match:

```text
actual decision lead = actual total-return value - benchmark total-return value
```

The benchmark terminal value is a measurement snapshot, not a sale in the continuous benchmark.

### Non-additivity rule

Never sum decision-benchmark values to calculate portfolio value, portfolio gain/loss, charts, or portfolio XIRR. Recycled capital can create multiple sequential decision benchmarks without representing new outside capital.

If `$1,000` in XYZ is sold for `$1,500` and funds a `$1,500` ABC purchase, the decision benchmarks start with `$1,000` and `$1,500`, but cumulative outside capital may remain `$1,000`.

Decision records may be aggregated only into clearly decision-level statistics such as win rate, median lead/lag, batting average, slugging, and distributions of decision XIRR.

## Worked examples

The examples omit dividends and later market movement.

### Actual holding loses while the index gains

```text
Initial actual buy:                         $1,000 XYZ
Initial external contribution:             $1,000
Initial VOO portfolio contribution:        $1,000
XYZ:VOO decision benchmark:                $1,000 notional

XYZ sale proceeds:                           $500 cash
VOO portfolio benchmark at sale:           $1,200 and remains invested
XYZ decision result:                         -$500
XYZ:VOO decision result:                     +$200
XYZ decision behind VOO:                     $700
```

The next actual purchase costs `$1,000`:

```text
Actual cash used:                            $500
New external contribution:                   $500
Actual value after purchase:               $1,000

VOO receives the same contribution:          $500
VOO portfolio value:                       $1,700

Cumulative contribution to each:           $1,500
Actual behind VOO:                           $700
```

A separate `$1,000` decision benchmark is created for the new buy but is not added to the `$1,700` portfolio benchmark.

### Actual holding gains more than the index

```text
Initial contribution to each:              $1,000
XYZ sale proceeds:                         $1,500
VOO portfolio value:                       $1,200 and remains invested
Actual portfolio lead:                       $300
XYZ decision lead:                           $300
```

If actual cash funds a `$1,500` ABC buy:

```text
New external contribution:                     $0
VOO portfolio contribution:                    $0
VOO portfolio remains:                     $1,200 invested
ABC:VOO decision benchmark:                $1,500 notional
```

The portfolio comparison stays `$1,500` versus `$1,200`. The `$1,500` decision benchmark evaluates ABC only.

### Dividends reduce later contributions

Before a `$1,000` actual buy:

```text
Actual dividend cash:                         $100
VOO dividend cash:                             $50
Actual external contribution:                 $900
```

Both portfolio benchmarks receive `$900`. At the buy event, VOO invests its `$50` dividend cash plus the `$900` contribution, or `$950`. It does not receive an extra `$50` to match the actual purchase. The actual buy still receives a separate `$1,000` VOO decision benchmark.

## XIRR

### Portfolio XIRR

Use exact-date XIRR, not periodic IRR or an aggregate CAGR shortcut.

For actual, VOO, and QQQ:

```text
each external contribution: negative flow on its actual date
terminal portfolio value:   positive flow on the valuation date
```

Sales, retained dividends, and cash-funded purchases are internal and do not appear separately. Solve for `r`:

```text
sum(cash_flow_i / (1 + r)^((date_i - first_date) / 365)) = 0
```

All three portfolio XIRRs use identical negative flows and their respective terminal values, making them directly comparable.

### Decision XIRR

For each actual or benchmark decision:

```text
original investment:                negative flow on buy date
each attributed distribution:      positive flow on its event date
sale proceeds or current security
value:                              positive terminal flow
```

The terminal date is actual sale date when closed and valuation date while open. Closed decision XIRRs freeze.

Distributions represented as intermediate positive XIRR flows must not also be added to the terminal XIRR flow. The terminal XIRR flow contains only sale proceeds or terminal security value. The separate `total-return value` used for dollar comparison may equal terminal security value plus cumulative distributions.

### Solver and display

- Require at least one negative and one positive flow.
- Require terminal date later than first flow date.
- Aggregate same-day flows before solving.
- Use a deterministic irregular-date solver with documented tolerance and iteration limits.
- Do not silently clamp extreme valid results.
- Return unavailable with a diagnostic reason if no valid solution is found.
- Display annual percentage to two decimals and retain greater internal precision.
- Label portfolio results `Since-inception XIRR` and decision results `Decision XIRR`.
- Do not reuse the existing single-start/single-terminal `compute_irr()` helper for portfolio XIRR.

## Required derived records

Exact filenames may follow project conventions, but these logical records are required.

### Actual lot state

- lot and buy transaction IDs;
- buy date, ticker, price, shares, and original investment;
- adjusted shares and open/closed status;
- sell ID, date, price, shares, and proceeds when closed;
- distributions through valuation or sale;
- current security value when open;
- frozen decision result when closed; and
- schema and replay-generation versions.

### Actual cash-event ledger

- event ID, date, and deterministic within-day sequence;
- source transaction or market-event ID;
- type: `DIVIDEND`, `SALE_PROCEEDS`, `EXTERNAL_CONTRIBUTION`, or `PURCHASE`;
- signed amount, cash before, and cash after; and
- related lot ID when applicable.

### Portfolio-benchmark state and events

For VOO and QQQ separately:

- current shares and cash;
- cumulative external contributions;
- every copied contribution and triggering actual buy;
- every dividend cash event;
- every contribution/dividend reinvestment;
- actual event date, resolved benchmark market date, benchmark close, share change, and cash used;
- current security and total value; and
- enough provenance to prove actual sells generated no benchmark sales.

### Decision-benchmark state

- decision ID, actual lot ID, and benchmark ticker;
- original investment, buy date, start market date, start price, and initial shares;
- adjusted shares and attributed distributions through terminal;
- open/frozen status;
- terminal event date, terminal market date, price, security value, distributions, and total-return value;
- dollar gain/loss and lead/lag; and
- decision XIRR and calculation status.

### Daily portfolio values

For each chart date:

- actual cash, security value, and total value;
- VOO cash, security value, and total value;
- QQQ cash, security value, and total value;
- daily and cumulative external contributions;
- actual-versus-VOO and actual-versus-QQQ differences; and
- provisional/settled status.

Cache validity depends on a ledger-content fingerprint, accounting-policy version, dividend/split versions, and valuation-data version, not row count.

## UI and reporting

### Portfolio summary

For actual, VOO, and QQQ show:

- total value;
- securities value;
- cash;
- cumulative external contributions;
- total dollar gain/loss;
- since-inception XIRR; and
- valuation date.

Headline comparisons use continuous values:

```text
Actual is $X ahead of/behind VOO
Actual is $Y ahead of/behind QQQ
```

### Holdings and decisions

- Actual Holdings contains open actual positions only.
- Portfolio Benchmark Holdings contains continuous index shares and dividend cash.
- Decision benchmarks are never presented as portfolio holdings.
- Group an actual buy and optional sell as one decision record.
- Show lot ID, dates, original investment, status, security value/proceeds, distributions, actual result/XIRR, equal-dollar VOO and QQQ results/XIRRs, and dollar lead/lag.
- Mark closed decisions visibly as frozen.
- Source sells may also appear separately for audit without breaking the buy-lot grouping.

### Charts and today's change

- The primary chart plots actual, continuous VOO, and continuous QQQ portfolio values.
- An optional contribution overlay is allowed.
- Never plot summed decision benchmarks as a portfolio series.
- Only open securities affect today's market change; cash has zero change in v1.
- Closed actual lots and frozen decisions do not appear as current movers.

## Statistics

Transaction win/loss, batting average, and slugging use decision benchmarks:

- open decisions compare through valuation;
- closed decisions compare frozen results through sale;
- starting investments match exactly;
- distributions count for both sides;
- closed classifications never change; and
- records match by stable lot ID.

Portfolio dollar lead/lag and XIRR use continuous benchmarks. Decision statistics need not reconcile arithmetically to portfolio results because they answer a different question and reuse capital.

## Validation and failure behavior

Reject replay before replacing state for:

- missing columns or invalid dates, actions, tickers, IDs, prices, or shares;
- duplicate transaction IDs;
- a buy with nonblank `LOT_ID`;
- a sell with missing/unknown `LOT_ID` or referencing a sell, later buy, or same-day unprocessed buy;
- ticker mismatch, sale before purchase, or already-closed lot;
- sell shares unequal to the complete adjusted actual lot;
- duplicate or inconsistent dividend/split events;
- missing valid VOO/QQQ benchmark price required for a contribution investment, dividend reinvestment, decision start, or terminal snapshot;
- negative cash after posting;
- a contribution not copied identically to both portfolio benchmarks;
- a portfolio-benchmark sale caused by an actual sell;
- any decision value entering portfolio state;
- failure to reproduce cash, shares, contributions, or value from event records; or
- failure writing any member of a new generation.

Errors identify portfolio ID, transaction/event ID, lot ID where applicable, failed invariant, and corrective action. Refresh preserves and serves the last-known-good generation.

## Atomic persistence and recovery

1. Parse and validate source and market data.
2. Resolve all required benchmark prices.
3. Replay actual cash and lots in memory.
4. Replay both continuous portfolio benchmarks.
5. Build all decision benchmarks.
6. Calculate summaries, daily values, statistics, and XIRRs.
7. Validate cross-view invariants.
8. Write the complete accounting snapshot to a temporary file and atomically replace `accounting_snapshot.json` only after validation succeeds.
9. Build the daily-value cache completely in memory, write its data and fingerprint sidecar through temporary files, and replace each completed artifact atomically.
10. On replay failure, continue serving the prior accounting snapshot with a visible warning. A chart cache may be rebuilt independently because it is derived entirely from the same source and market facts.

Derived files are caches and projections. The source ledger plus versioned market-data facts are authoritative.

## Migration and rollout

### Source migration

1. Back up source ledgers and persistent Render data.
2. Add ledger-schema and accounting-policy versions.
3. Rewrite each legacy row as `BUY` without changing entered date, ticker, price, or shares.
4. Assign permanent buy IDs in existing source-row order.
5. Preserve input share precision.
6. Validate every migrated ledger before replacing its source.

### Derived-state migration

1. Treat legacy `shadow_voo.csv` and `shadow_qqq.csv` as reconciliation inputs only.
2. Rebuild actual cash from zero, crediting historical dividends and using cash before inferring later contributions.
3. Build continuous portfolio benchmarks from the inferred actual contribution stream.
4. Build separate decision benchmarks for every legacy buy.
5. Rebuild daily values and statistics under the new policy version.
6. Do not transform legacy shadow rows in place; they combine portfolio and decision concepts.

Historical headline values may change because legacy behavior treated every buy as newly invested and accumulated dividends outside spendable cash. Reconciliation must attribute differences to dividend cash consumed, contribution reclassification, precision correction, or benchmark-model replacement.

### Rollout

1. Implement on a feature branch from `staging`.
2. Run the complete automated suite locally.
3. Run migration in report-only mode on copies of every portfolio.
4. Review contribution timelines, cash ledgers, benchmark values, and decision counts.
5. Perform local UAT for losing, winning, dividend-funded, and fully cash-funded purchases.
6. Deploy to staging and rebuild its persistent derived data.
7. Complete staging reconciliation and UAT.
8. Back up production persistent data immediately before promotion.
9. Promote through staging to main and verify every portfolio after its first replay.

## Test matrix

### Ledger, replay, and migration

- buy-only rows migrate without changing entered facts;
- IDs are unique, stable, and deterministic;
- source order may differ from chronological replay order;
- inserting/correcting an old event causes a correct full replay;
- unchanged inputs reproduce value-equivalent state;
- stale schema/policy versions force a rebuild;
- non-trading event dates use and record the most recent prior settled close within seven days;
- unavailable or stale benchmark prices beyond that window fail atomically;
- legacy shadow files never become authoritative portfolio benchmarks.

### Cash and contributions

- first buy with zero cash contributes its full amount;
- buy fully funded by sale cash contributes zero;
- partial cash funding contributes only the shortfall;
- dividend cash is consumed before new capital;
- same-day buys consume cash in source order;
- sells precede buys on the same date;
- cash never becomes negative;
- cumulative contributions never decrease on sale;
- terminal value includes remaining cash.

### Complete-lot sales

- profitable, break-even, and losing full-lot sales;
- two lots of one ticker with only the referenced lot closed;
- multiple sales on one date;
- one/multiple splits and a split on sale date;
- partial, excessive, duplicate, mismatched, and pre-purchase sales rejected;
- entered execution price determines proceeds;
- future events cannot change frozen sale facts;
- a sale generates no continuous portfolio-benchmark transaction.

### Continuous portfolio benchmarks

- every contribution is copied exactly and on the same date;
- cash-funded actual buys create no benchmark contribution;
- index principal remains invested across actual sales;
- benchmark dividends enter independent benchmark cash;
- pending dividend cash reinvests at the next actual buy;
- contributions always match across all three portfolios;
- raw value difference equals gain/loss difference;
- no synthetic catch-up cash, liability, or value adjustment exists.

### Decision benchmarks

- every actual buy creates exactly one VOO and one QQQ decision benchmark;
- each starts with the exact actual investment;
- cash-funded buys still create decision benchmarks;
- open comparisons update through valuation;
- closed comparisons freeze on sale;
- distributions attribute independently;
- terminal snapshots do not sell continuous shares;
- decision values never enter portfolio totals or XIRR;
- sequentially reused capital does not inflate portfolio value.

### Dividends, splits, and XIRR

- pre-buy and same-day-buy dividends excluded;
- post-buy and same-day-sale dividends included;
- post-sale dividends excluded from closed decisions;
- dividend shares reflect effective splits;
- actual dividends credit cash once and attribute analytically once;
- portfolio dividends use continuous shares, not decision records;
- dividends consumed later reduce contributions but remain in decision return;
- one contribution/terminal value produces expected XIRR;
- irregular contributions use exact dates;
- all portfolio XIRRs use identical negative flows;
- sales and retained dividends are not portfolio-XIRR external flows;
- terminal value includes cash and securities;
- decision XIRR includes dated distributions and terminal value;
- same-day flows aggregate;
- invalid/no-solution cases return unavailable with diagnostics;
- extreme valid rates are not silently clamped.

### UI and recovery

- summaries distinguish cash, securities, contributions, gain/loss, and XIRR;
- headline comparisons use continuous portfolio values;
- transaction rows use decision benchmarks;
- labels never imply decision benchmarks are assets;
- charts use continuous benchmark series only;
- closed decisions do not appear in holdings or today's movers;
- validation, pricing, calculation, and write failures preserve the last valid generation;
- recovery from source plus market data reproduces validated state.

## Deferred scope

- partial actual-lot sales;
- FIFO, LIFO, or automatic lot selection;
- external withdrawals or independent deposits;
- taxes, commissions, fees, and bid/ask spread;
- interest or yield on cash;
- automatic same-day dividend reinvestment or DRIP;
- time-weighted portfolio return display;
- sell-decision hindsight analysis after the sale date;
- shorts, options, margin, leverage, and liabilities;
- intraday benchmark execution prices; and
- same-day buy-and-sell round trips.

Daily values and contribution history must retain enough information to add time-weighted return later without changing source transactions.

## Acceptance criteria

Version one is complete only when:

1. A sell closes one explicitly identified complete actual lot and credits proceeds to actual cash.
2. An actual sell never mutates a continuous VOO or QQQ portfolio benchmark.
3. Every actual buy consumes cash before inferring external capital.
4. Every inferred contribution is copied exactly to VOO and QQQ on the same date.
5. Actual and portfolio-benchmark cumulative contributions always match.
6. Portfolio dollar lead/lag equals the difference between continuous portfolio values.
7. Every actual buy has separate equal-dollar VOO and QQQ decision benchmarks.
8. Decision benchmarks never contribute to portfolio holdings, value, cash, charts, or XIRR.
9. Closed actual and decision records freeze at the actual sale date.
10. Dividends enter their respective cash accounts and follow the cash-first policy.
11. Actual, VOO, and QQQ since-inception XIRRs use identical contribution flows and respective terminal values.
12. Decision XIRRs use correct dated purchase, distribution, and terminal flows.
13. Holdings, cash, contributions, decisions, summaries, charts, statistics, and XIRRs reconcile to replay.
14. Every material migration difference is explained by the new accounting rules.
15. Invalid input or unavailable data cannot partially replace state.
16. The full automated suite and local and staging UAT pass.
