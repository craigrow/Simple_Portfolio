# Shared pricing implementation and local validation

Branch: `feature/shared-pricing`, based on the clean main-derived resilience branch.
Status: local implementation; not deployed.

## Boundaries

- `pricing.py`: pure immutable observation/result types, end-of-day resolution, deterministic source precedence, indefinite carry-forward, strict session-close queries, and split normalization. No provider or portfolio imports.
- `pricing_sources.py`: transaction/manual/cache adapters, per-instrument acquisition configuration, content-addressed read snapshots, SQLite generation/receipt audit records, and process locks. Unknown-basis legacy cache observations are retained but quarantined from valuation.
- `price_provider.py`: API acquisition, raw close normalization, backfills, provisional quote metadata per ticker, and non-destructive cache migration. No accounting imports.
- `portfolio_replay.py`: consumes the shared price book for holdings, benchmark contributions, decisions, and history. Missing benchmark inputs do not abort actual accounting.
- `portfolio_engine.py`: coordinates acquisition and accounting, preserves accepted observations if derived calculations fail, and prevents publishing results against a changed source ledger.
- `market_calendar.py`: offline exchange-session checks using the pinned pandas_market_calendars package. Package API reference: https://pandas-market-calendars.readthedocs.io/en/stable/usage.html .

## Configuration and compatibility

`config.json` accepts `pricing: {"TICKER": "api" | "static"}`. The default is API. Fundrise now explicitly marks Flagship and Income as static. The old `manual_pricing` portfolio flag is read only as a compatibility migration input; valuation never checks it. All current portfolio source ledgers use the shared replay path. Legacy helper APIs and CSV projections remain for backward compatibility, not as a new source of pricing policy.

Instrument identity is scoped to each portfolio's store. No cross-portfolio sharing is performed, so similarly named private investments cannot be accidentally merged. Cross-portfolio instrument aliases and shared acquisition deduplication are not introduced in this change.

The existing application is a daily valuation application. Quote effective dates remain date-only; exact intraday quote timestamps are not fabricated. `recorded_at` is the first time that observation was imported into the new store, not its historical market execution time. Price Updates distinguishes Price Date from First Stored (UTC). The pure daily resolver rejects intraday timestamp requests instead of silently interpreting them as end-of-day.

## Accounting and presentation

Transaction cash flows and lot accounting remain unchanged. Existing whole-lot sale validation is preserved; adding partial-lot sales is a separate accounting feature.

Transaction and manual observations support valuation even when every API request fails. A transaction observation does not become an official close. An unknown benchmark contribution price leaves that benchmark unavailable; a later quote does not silently reconstruct the missing purchase. Carried benchmark observations are labeled estimated.

The Price Updates table is generated from the same price book as the displayed snapshot and includes value, effective date, source, status, and first-recorded time. Values are normalized for intervening splits; the original value and split factor remain in the snapshot. An unavailable market-day comparison is omitted rather than displayed as a zero change. Static daily valuation series carry recorded values forward; their lack of daily closes does not prevent valuation.

## Persistence and rollback

Each portfolio gets `data/prices.sqlite3`, containing immutable observation generations and refresh attempt outcomes. Source CSVs remain authoritative migration inputs, and API cache CSVs remain compatible with existing deployment tooling. An unchanged source fingerprint can be loaded from its durable SQLite generation. Price records, including superseded generations, are not deleted on failed refresh.

`price_kinds.json` preserves per-ticker close/intraday provenance independently of the legacy per-date provisional sidecar. Source fingerprints include this metadata. A raw-price policy rebuild retains its original cache as `price_history.csv.pre-shared-pricing` before publishing replacements.

Refresh holds an exclusive per-portfolio lock; rendering holds a shared lock. Source fingerprints are checked before publication. Snapshot files are replaced atomically. Old source caches, ledgers, and manual updates are retained; no automatic database/schema cleanup or remote deployment occurs. The original checkout and unrelated edits remain preserved.

## Validation and UAT

The automated suite includes pure pricing precedence/date tests, static/API equivalence, missing benchmarks, complete outages with no cache, manual revisions, source changes during refresh, invalid provider values, split-normalized carry-forward, receipt timestamps, per-ticker provisional metadata, and module dependency checks.

`scripts/validate_price_outages.py` exercises isolated copies of all five full ledgers against recorded provider data in warm-cache, April-cache/no-snapshot, and raw-policy-rebuild cases, then applies a total outage to each warm case. It checks real holdings, ledger coverage, complete dashboard features, chart/snapshot reconciliation, and unchanged source inputs.

`scripts/local_uat.py live|outage VALIDATED_CACHE_DIRECTORY` serves isolated local copies and asserts that every configured portfolio is present and its source ledgers match the checkout. Live mode permits actual acquisition; outage mode blocks price and corporate-action acquisition. Dates in recorded fixtures are not claimed to be fresh live prices.

Promotion remains gated on review of the local UAT results, followed by explicit approval for Staging.

## Observed local validation results (November 17, 2026)

- Full automated suite: 264 tests passed (`python -m pytest tests/ -q`).
- All 15 full-ledger scenarios passed, including five subsequent total-provider-outage checks.
- Foolish Portfolio retained 64 holdings and 971 recorded-fixture chart points, with zero missing actual-value dates in all three scenarios.
- A real live refresh returned success for all five portfolios. The live UAT dashboards rendered without refresh warnings; the simulated-outage dashboards retained their expected cached-price warnings.
- Browser checks confirmed the static/API Price Updates table, full holdings/chart views, and no console errors. Missing current-session quotes no longer produce zero-change mover rankings.
- All five portfolio selections were verified on each local server. Staging and production were not modified.
