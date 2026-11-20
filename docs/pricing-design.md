# Shared pricing and unavailable-price handling

Status: proposed for review; not an implementation or deployment approval.
Date: November 17, 2026

## 1. Objective

Separate price acquisition and resolution from portfolio accounting. Every portfolio uses the same valuation engine. Investments can have API or static pricing, and a portfolio can contain both. A provider failure must not erase valid observations, alter transaction accounting, or prevent unrelated holdings and comparisons from being calculated.

The pricing layer returns a dated, attributable price or an explicit unavailable result. The calculation layer never fetches prices, chooses fallback sources, or branches on a portfolio name or pricing mode.

## 2. Current problems

The current code has portfolio-wide manual-pricing configuration, manual-price merging, API fetching, freshness checks, and valuation dependencies in portfolio_engine.py. portfolio_replay.py also resolves cached prices and imposes a seven-day limit on some benchmark-event prices. This distributes pricing policy across acquisition and accounting.

A new split could invalidate an entire cached ticker history, assuming the provider could reconstruct it. Missing history could then stop replay or chart generation. The current resilience branch mitigates these failures but does not implement the separation proposed here. Its tests and failure fixtures will be retained as regression evidence.

## 3. Boundaries and components

- Instrument registry: stable instrument identity, display ticker, currency, API symbol if applicable, and acquisition mode (API or static). Ticker alone is not sufficient identity for private investments with reused names. Pricing mode belongs to the instrument, not the portfolio.
- Source adapters: API quotes/history, transaction observations, and manual valuation updates. Adapters normalize data and do not calculate portfolio values.
- Price store: persists observations, revisions, source identity, retrieval status, and immutable published generations.
- Price resolver: a pure read interface over one store generation. Applies date eligibility, source precedence, freshness classification, and split normalization.
- Corporate-action service: supplies dated splits and dividend events. These are accounting inputs, not prices.
- Portfolio engine: replays buys, sells, contributions, cash, and corporate actions, then consumes resolved prices. Returns complete or explicitly partial results.
- Presentation layer: explains valuation dates, carried-forward prices, missing values, and refresh failures without exposing implementation details.

No network I/O occurs during page rendering or accounting replay. A refresh coordinator runs adapters, commits valid observations, and requests recalculation separately.

## 4. Observation model

Each observation records:

| Field | Meaning |
| --- | --- |
| observation_id | Stable identity for deduplication and revisions |
| instrument_id | Stable investment identity |
| price, currency | Positive finite decimal value and its currency |
| effective_at / effective_date | When the valuation applies |
| time_precision, timezone | Exact timestamp or date-only; never invent midnight as a known quote time |
| recorded_at | When the app stored the observation, in UTC |
| source | API, transaction, or manual |
| kind | Market close, intraday quote, execution price, manual valuation, or legacy unknown |
| source_reference | Provider identifier, transaction ID, or manual-update ID |
| revision / supersedes | Correction relationship without silently losing the prior record |
| adjustment_basis | Raw per-share basis and applicable split basis |

Cache is storage, not a source. Loading yesterday's quote today does not change its effective date. A failed fetch updates attempt metadata, not quote timestamps. Legacy data without exact receipt times remains explicitly unknown.

Store fetch attempts separately: instrument, attempted_at, requested range, outcome, and concise reason. A successful request with no usable new data is not a successful price update.

Use decimal arithmetic at calculation boundaries; do not round source observations to two decimals. Formatting is a presentation concern. Currency conversion is outside this change: incompatible currencies must be rejected or marked unavailable rather than combined.

## 5. Static and API acquisition

Static instruments accept buy and sell execution prices and manual valuation updates. They do not issue API requests and do not receive an API-staleness warning merely because their last valuation is old. Their valuation date remains visible.

API instruments accept market observations and also retain transaction/manual observations as fallbacks. A failed API request does not change the instrument's configured mode. Benchmarks are ordinary API instruments using the same store and resolver.

Manual updates need both an effective valuation date and an entry timestamp. They are dated observations, not permanent overrides of all later quotes. Explicit override/correction workflows, if needed later, must be distinct from ordinary manual updates.

Multiple portfolios can share a market instrument. Private or independently valued investments must retain separate instrument IDs unless explicitly mapped to the same investment. Sharing observations must not accidentally expose or mix unrelated private valuations.

## 6. Resolver contract and selection rules

Proposed interface:

    resolve(instrument_id, valuation_at, purpose, generation) -> PriceResult
    resolve_many(requests, generation) -> results

Purposes are VALUATION and SESSION_CLOSE. Portfolio code requests the appropriate purpose but does not implement its source or age rules.

PriceResult contains value (nullable), effective date/time, observation ID, source/kind, age, carried_forward flag, availability (available or unavailable), freshness (current, stale, not_applicable, or unknown), reason, and split normalization details. Fetch failure and valuation availability are independent: an API can fail while a usable cached price exists.

For VALUATION:

1. Use only valid observations effective at or before the requested valuation date/time. Never backfill an earlier date with a later price.
2. Select the latest eligible effective date/time. On a later date, a new static/transaction observation can supersede an older market observation.
3. For date-only end-of-day conflicts, prefer an official market close, then an API intraday observation, then an explicit manual valuation, then a transaction observation. A known timed observation must be eligible for the requested cutoff; do not select a later close for a morning valuation.
4. Multiple same-day transaction observations without execution times use stable ledger order, not filesystem order. Manual revisions use explicit revision order. Preserve all source records so the decision is auditable.
5. Carry the selected observation forward until another valid observation arrives. Age alone does not make a valuation unusable. Classify API freshness using the relevant trading calendar and session; static freshness is not applicable.
6. If nothing qualifies, return unavailable with a reason. Do not substitute zero or look forward in time.

Date-only observations support end-of-day valuation; they cannot establish an exact intraday execution sequence. Existing charts remain end-of-day series, with today's provisional point labeled accordingly.

For SESSION_CLOSE, require an official close for the requested session. A generic last price does not become a close. If absent, return unavailable. This supports accurate daily market-change reporting without preventing ordinary valuation.

Default historical output is a restated history using all currently known observations with eligible effective dates. Reproducing what the app knew at a prior recording time would require an additional recorded_at cutoff; retain the metadata but defer that user-facing feature.

## 7. Splits and dividends

Keep source observations immutable. A pre-split price carried forward past a split must be converted to the target date's share basis, while accounting adjusts quantities once. Example: 10 shares at $100 followed by a 4:1 split become 40 shares at a carried-forward $25, preserving $1,000 value.

Use raw, unadjusted market prices with explicit split events. Provider-adjusted observations must be normalized on ingestion or quarantined when their basis cannot be established. Never mix adjusted prices with separately adjusted shares.

A new split invalidates dependent derived valuations, not the raw price history. Empty split or dividend responses must not delete known events. Corrections require identifiable replacements and recalculation from the affected date. Dividends remain cash events; avoid adjusted-price total-return double counting.

## 8. Portfolio behavior

Transaction execution prices determine cash, cost basis, sale proceeds, and realized gains regardless of valuation-price availability. Sale observations may update the remaining investment's valuation; a fully closed lot's terminal proceeds come from its actual sale.

For each valuation date, resolve prices for open holdings and required benchmarks. Record dependencies and price provenance in the result.

- All holdings priced: show the total, even if some observations are old, with coverage and date information.
- Some holdings unpriced: total value and dependent returns are unavailable. Show known priced subtotal, cash, and affected holdings explicitly; never label the subtotal as the portfolio total.
- Missing actual historical value: leave a gap for that date, retain independently calculable benchmarks and other dates.
- Missing benchmark input: keep actual accounting available. Mark only the affected benchmark path and comparisons unavailable. Do not silently skip a contribution and resume with an understated benchmark balance.
- A cached benchmark price may price a hypothetical contribution under VALUATION, but the resulting comparison must retain an estimated/carried-price flag. If no prior observation exists, the comparison remains unavailable until repaired.
- A new transaction must not be hidden by serving an older snapshot as if it included the new ledger. A last-known-good snapshot may be shown separately with its ledger cutoff and valuation date.

Historical charts carry static prices forward; they do not create gaps merely because there was no new observation that day. Missing data before the first observation remains a gap.

## 9. Daily changes and statistics

Today's market change and market mover rankings use SESSION_CLOSE baselines and a usable current-session price. Missing current-session observations must not produce a fabricated zero daily change. Static instruments normally have no such comparison and display “Not available” for that metric.

Do not label a market-priced subset's daily change as the entire portfolio's change. Either the complete requested metric is available, or show its limited coverage explicitly.

Daily valuation history can include carried static valuations. Return statistics based on that history must disclose that carried valuations smooth observed returns. Do not forward-fill missing portfolio totals merely to calculate returns, and do not compute a one-day return across a missing-date gap. Contributions and withdrawals continue to be handled by the existing return methodology; this change does not redefine accounting returns.

## 10. Refresh, persistence, and recovery

Prefer a normalized SQLite store using Python's standard library, on each environment's persistent disk. Keep transaction and manual-update source files authoritative during migration. SQLite transactions provide atomic observation batches; generated portfolio snapshots remain separate versioned artifacts.

A refresh performs these steps:

1. Capture source-ledger/config fingerprints and the current store generation.
2. Ingest new transaction and manual observations idempotently.
3. Request API data independently per instrument or batch; validate responses and retain successful records when other requests fail.
4. Commit accepted observations and attempt outcomes in a transaction. Never clear usable prices before fetching replacements, including on force refresh.
5. Calculate against one immutable generation and ledger fingerprint. Tag output with both.
6. Publish a coherent derived snapshot atomically if the fingerprints still match. If concurrent edits occurred, retry rather than publish mixed versions.

A calculation failure does not roll back valid newly acquired source observations. Retain the prior derived snapshot as explicitly outdated, and expose the new calculation error separately. Serialize overlapping refresh jobs or use generation checks to avoid lost updates. Resolve database path and initialization identically for local, Staging, and production; back up before migration.

## 11. User interface

Keep the existing dashboard tabs and the new Price Updates tab. Extend its table to include ticker, latest valuation price, effective quote date/time, source, and status. Exact times appear only when known; show date-only observations honestly. An optional detail view can show last fetch attempt and last successful receipt time, which are different from valuation time.

Use a concise dashboard summary of stale/missing valuation inputs. Static instruments are labeled static, with their last valuation date, rather than “failed refresh.” Separate missing historical coverage from stale current prices. Table values must come from the same resolved generation as the displayed portfolio, not from unrelated newer cache rows.

A global freshness stamp cannot claim that all instruments were updated together. Show portfolio valuation date plus coverage, with per-instrument dates in Price Updates.

## 12. Migration and rollout

1. Freeze the current resilience branch as a reference; do not deploy it as the architectural solution.
2. Add observation types, store, resolver, and adapter interfaces behind an internal switch. Implement deterministic pricing tests first.
3. Migrate portfolio manual-pricing flags into explicit instrument modes, preserving existing VCMoney/static behavior and mixed benchmark pricing. Remove portfolio-specific branches after parity is established.
4. Import transactions, manual updates, compatible raw caches, and corporate actions. Retain source IDs. Import ambiguous legacy prices as unknown provenance/basis, not invented official closes; exclude incompatible bases from resolution until reconciled.
5. Run both implementations on copied full ledgers. Compare holdings, cash, contributions, realized gains, benchmark calculations, and chart dates. Explain intentional changes, including transaction fallbacks replacing previously missing values.
6. Move replay, charts, stats, refresh status, and Price Updates onto the shared interface. Remove direct price CSV reads and API calls from calculation code.
7. Run local UAT for all five portfolios and the full outage matrix. Only after user approval deploy to Staging; production requires separate verified promotion.

Keep the old data files and a migration backup for rollback. Rollback switches the application/store version; it must not discard newly entered source transactions or manual valuations. Re-import those source records when restoring an older compatible version. Do not delete the old cache until the new path is accepted.

## 13. Test and acceptance plan

Unit tests cover timestamp eligibility, date-only semantics, deterministic precedence, duplicate observations, invalid/nonfinite prices, manual corrections, stale API fallbacks, static carry-forward, missing prices, and exact-session close requests. Verify effective dates and recorded times never become interchangeable.

Accounting integration tests cover buy/sell observations, partial sales, entirely closed positions, dividends, split-adjusted carry-forward, benchmark contributions with stale or absent prices, and partial valuation coverage. Assert actual accounting remains available when only a benchmark is missing.

Failure tests cover empty/partial provider responses, exceptions, rate limits, invalid history, new split with unavailable replacement history, missing corporate-action responses, forced refresh failure, interrupted writes, concurrent refreshes, and ledger changes during calculation.

Architecture checks ensure valuation/replay modules cannot import provider adapters or perform network I/O, and portfolio names/modes do not alter calculation algorithms. Swapping a valid observation's source from API to static must preserve equivalent valuation results; only freshness/source labels may differ.

End-to-end tests use isolated copies of all five portfolios, verify the exact portfolio registry and selected portfolio, and exercise holdings, transactions, charts, benchmarks, stats, and Price Updates. Test no-cache, warm-cache, old-cache, complete outage, partial outage, and mixed static/API portfolios. Confirm source files remain unchanged.

Release gates:

- No fabricated zero values or current timestamps for unavailable observations.
- No future observations used in earlier historical valuations.
- No loss of valid cached observations during failed refresh or split ingestion.
- Static last-price valuation works without any closing price.
- Unavailable benchmark data does not break actual holdings/accounting.
- Published totals reconcile with priced holdings and cash, or explicitly report incomplete coverage.
- All existing relevant tests plus the new matrix pass; full-data differences are reviewed before local UAT and Staging approval.

## 14. Decisions proposed for review

Approve two acquisition modes at instrument level, one shared valuation resolver, indefinite last-price carry-forward with transparent dates, and a separate strict session-close query for daily market metrics. Approve transaction observations as fallback valuations for API instruments as well as static instruments. Approve the same-day precedence in section 6 and explicit estimated benchmark comparisons when using carried prices.

Implementation sizing and a final migration inventory follow design review. This document does not authorize a deployment.
