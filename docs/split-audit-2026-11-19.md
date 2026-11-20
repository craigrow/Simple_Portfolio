# Split-history audit — November 19, 2026

All implementation and reconciliation work is local. No redesign, safeguards, or audit corrections have been pushed to staging or production.

## Findings

- Audited all **80 current holdings** in five portfolios, plus benchmark and historical ledger securities.
- **80/80 share quantities reconcile** between a separate Decimal-based ledger roll-forward and the application engine.
- **11/11 split events affecting current holdings are independently confirmed**, with issuer/SEC/OCC references in `audit/split_evidence.json`.
- No additional split event or conflicting ratio was found in the live provider retrieval.
- Provider coverage through the November 18 settled session: **72 holdings checked**, **six not applicable** (four spot cryptocurrencies and two static funds), **two require review**.
- This is a checked data set, not a mathematical proof that no unreported event exists. Agreement with the ledger tests calculations; independent event notices support the supplied split history. Brokerage records are not required for this audit.

## Review exceptions

**EMBJ:** The provider omits the November 16 daily price row, the same day as the ADS ratio change. The split event itself is present locally and independently confirmed as 4:1 by Embraer. The engine uses 123.586024 shares. Preserve the event; do not fabricate the missing price or mark daily provider coverage complete. The apparent fourfold price jump between November 13 and 17 is explained by the recorded November 16 split.

**VCX:** The ledger begins January 16, 2025; public market data starts March 19, 2026. Fundrise confirms the March 19 listing. The prior period is private-fund ownership, so it cannot have complete exchange-session coverage. The price-discontinuity scan flags the switch from the private valuation series to market trading on listing day. It is a review candidate, not evidence of a reverse split. No split or transaction was invented. The private-period coverage classification remains open for UAT.

## Reproducing staging's discrepancy locally

On a disposable copy, removed only CRWD's 2026 4:1 event, MNST's 2026 2:1 event, and EMBJ's 2026 4:1 event while retaining a fresh split-file timestamp and prior coverage metadata. Refresh detected the changed event set, re-fetched history and restored all three events.

With November 18 prices held constant, Foolish Portfolio value changed from **$163,658.72** to **$174,280.95**, exactly matching the intact reference. Every holding matched and price-history bytes were unchanged. This demonstrates local recovery; staging's actual disk has not been repaired or claimed reconciled.

## Safeguards

- Explicit full-history requests from the earliest ledger date through the last settled session, repeated when coverage is stale, the ledger expands, or the cached event set changes.
- Separate per-security attempts, successful coverage bounds, response hashes and reported event lists. File modification time is not a freshness signal.
- Independent retries; one failed security does not discard other successful results.
- Empty/partial histories, missing sessions, invalid ratios, contradictory ratios and omitted known events remain reviewable. Known events survive failures and conflicting replacements are quarantined.
- Dashboard warning and expandable per-security coverage table. Successful quote retrieval cannot hide failed split verification.
- Independent evidence checks flag missing or conflicting issuer-confirmed events.
- Local audit command checks all ledger-derived quantities and flags split-like price discontinuities without creating events automatically.

## Reproduction

```sh
python -m pytest tests/ -q
python scripts/audit_splits.py /path/to/portfolios /new/output/directory --as-of 2026-11-18 --fetch
```

The audit command copies the input portfolios, performs acquisition and replay only in the new output directory, preserves all source-file hashes, and writes `report.json` and `report.md`. `--fetch` is optional when examining an already audited copy. The report contains all holdings, coverage failures, event evidence and jump candidates.

## Validation and UAT

- Automated suite: **280 tests passed** (33.05 seconds).
- Full-ledger old-cache and outage matrix: **15 scenarios passed, plus five total-outage checks**, with no reconciliation issues; validation input hashes remained unchanged. Both local previews returned HTTP 200 with holdings and split coverage for all five portfolios.
- Browser verified the warning, normal holdings/chart dashboard and expandable split-coverage table.
- Local UAT: http://127.0.0.1:5003/ ; outage preview: http://127.0.0.1:5004/ .
- Deployment remains on hold until exceptions and user UAT are accepted. No account-by-account brokerage export is required.

## Holding-by-holding reconciliation

| Portfolio | Holding | Ledger shares | Engine shares | Provider status | Applicable events confirmed |
| --- | --- | ---: | ---: | --- | --- |
| foolish_portfolio | AAPL | 10.376550 | 10.37655 | provider_checked | 0/0 |
| foolish_portfolio | ABNB | 16.658086 | 16.658086 | provider_checked | 0/0 |
| foolish_portfolio | AMZN | 21.386414 | 21.386414 | provider_checked | 0/0 |
| foolish_portfolio | APP | 17.660 | 17.66 | provider_checked | 0/0 |
| foolish_portfolio | ASML | 6.637 | 6.637 | provider_checked | 0/0 |
| foolish_portfolio | AVGO | 7.00 | 7.0 | provider_checked | 1/1 |
| foolish_portfolio | AXP | 0.454035 | 0.454035 | provider_checked | 0/0 |
| foolish_portfolio | BROS | 74.269894 | 74.269894 | provider_checked | 0/0 |
| foolish_portfolio | CEF | 7.995 | 7.995 | provider_checked | 0/0 |
| foolish_portfolio | CPNG | 106.541 | 106.541 | provider_checked | 0/0 |
| foolish_portfolio | CRWD | 40.5878800 | 40.58788 | provider_checked | 1/1 |
| foolish_portfolio | DASH | 21.658639 | 21.658639 | provider_checked | 0/0 |
| foolish_portfolio | DDOG | 18.443 | 18.443 | provider_checked | 0/0 |
| foolish_portfolio | DOCU | 2.956235 | 2.956235 | provider_checked | 0/0 |
| foolish_portfolio | ELF | 5.826 | 5.826 | provider_checked | 0/0 |
| foolish_portfolio | EMBJ | 123.5860240 | 123.586024 | needs_review | 1/1 |
| foolish_portfolio | EME | 7.093 | 7.093 | provider_checked | 0/0 |
| foolish_portfolio | ENPH | 9.772690 | 9.77269 | provider_checked | 0/0 |
| foolish_portfolio | FIX | 2.931 | 2.931 | provider_checked | 0/0 |
| foolish_portfolio | FPS | 98.056 | 98.056 | provider_checked | 0/0 |
| foolish_portfolio | GEHC | 11.883 | 11.883 | provider_checked | 0/0 |
| foolish_portfolio | GLW | 5.514293 | 5.514293 | provider_checked | 0/0 |
| foolish_portfolio | GRAB | 401.016 | 401.016 | provider_checked | 0/0 |
| foolish_portfolio | HIMS | 3.701 | 3.701 | provider_checked | 0/0 |
| foolish_portfolio | HOOD | 7.563836 | 7.563836 | provider_checked | 0/0 |
| foolish_portfolio | HWM | 23.727 | 23.727 | provider_checked | 0/0 |
| foolish_portfolio | INTC | 19.646 | 19.646 | provider_checked | 0/0 |
| foolish_portfolio | IONQ | 6.956034 | 6.956034 | provider_checked | 0/0 |
| foolish_portfolio | KNSL | 6.470 | 6.47 | provider_checked | 0/0 |
| foolish_portfolio | KRYS | 3.218 | 3.218 | provider_checked | 0/0 |
| foolish_portfolio | LRCX | 9.1500 | 9.15 | provider_checked | 1/1 |
| foolish_portfolio | MA | 3.989912 | 3.989912 | provider_checked | 0/0 |
| foolish_portfolio | MELI | 4.524597 | 4.524597 | provider_checked | 0/0 |
| foolish_portfolio | META | 1.355 | 1.355 | provider_checked | 0/0 |
| foolish_portfolio | MNST | 74.6102720 | 74.610272 | provider_checked | 1/1 |
| foolish_portfolio | MSFT | 13.787362 | 13.787362 | provider_checked | 0/0 |
| foolish_portfolio | NFLX | 46.083700 | 46.0837 | provider_checked | 1/1 |
| foolish_portfolio | NVDA | 43.1704660 | 43.170466 | provider_checked | 1/1 |
| foolish_portfolio | PGR | 12.239 | 12.239 | provider_checked | 0/0 |
| foolish_portfolio | PRYMY | 72.0 | 72.0 | provider_checked | 0/0 |
| foolish_portfolio | QBTS | 1.060082 | 1.060082 | provider_checked | 0/0 |
| foolish_portfolio | RACE | 2.671 | 2.671 | provider_checked | 0/0 |
| foolish_portfolio | RBLX | 32.148168 | 32.148168 | provider_checked | 0/0 |
| foolish_portfolio | RDDT | 1.424 | 1.424 | provider_checked | 0/0 |
| foolish_portfolio | RELY | 112.222 | 112.222 | provider_checked | 0/0 |
| foolish_portfolio | RGTI | 11.96 | 11.96 | provider_checked | 0/0 |
| foolish_portfolio | RKLB | 60.369 | 60.369 | provider_checked | 0/0 |
| foolish_portfolio | SHOP | 30.640093 | 30.640093 | provider_checked | 0/0 |
| foolish_portfolio | SNOW | 0.392879 | 0.392879 | provider_checked | 0/0 |
| foolish_portfolio | SPOT | 1.694 | 1.694 | provider_checked | 0/0 |
| foolish_portfolio | STN | 44.116 | 44.116 | provider_checked | 0/0 |
| foolish_portfolio | STRL | 5.041 | 5.041 | provider_checked | 0/0 |
| foolish_portfolio | TMC | 18.08 | 18.08 | provider_checked | 0/0 |
| foolish_portfolio | TOST | 154.554 | 154.554 | provider_checked | 0/0 |
| foolish_portfolio | TSLA | 11.625107 | 11.625107 | provider_checked | 0/0 |
| foolish_portfolio | TSM | 18.615151 | 18.615151 | provider_checked | 0/0 |
| foolish_portfolio | TTAN | 19.329 | 19.329 | provider_checked | 0/0 |
| foolish_portfolio | U | 104.296 | 104.296 | provider_checked | 0/0 |
| foolish_portfolio | UBER | 15.488750 | 15.48875 | provider_checked | 0/0 |
| foolish_portfolio | V | 10.689917 | 10.689917 | provider_checked | 0/0 |
| foolish_portfolio | VEEV | 26.193678 | 26.193678 | provider_checked | 0/0 |
| foolish_portfolio | WING | 13.197 | 13.197 | provider_checked | 0/0 |
| foolish_portfolio | XYZ | 0.291566 | 0.291566 | provider_checked | 0/0 |
| foolish_portfolio | ZG | 0.484782 | 0.484782 | provider_checked | 0/0 |
| crypto_portfolio | BTC-USD | 0.02333097 | 0.02333097 | not_applicable | 0/0 |
| crypto_portfolio | DOGE-USD | 200.68 | 200.68 | not_applicable | 0/0 |
| crypto_portfolio | ETH-USD | 0.055132359 | 0.055132359 | not_applicable | 0/0 |
| crypto_portfolio | FBTC | 0.507 | 0.507 | provider_checked | 0/0 |
| crypto_portfolio | FETH | 37.330570 | 37.33057 | provider_checked | 0/0 |
| crypto_portfolio | ZEC-USD | 0.982854 | 0.982854 | not_applicable | 0/0 |
| fundrise_portfolio | Flagship | 3150.425972 | 3150.425972 | not_applicable | 0/0 |
| fundrise_portfolio | Income | 941.884681 | 941.884681 | not_applicable | 0/0 |
| tqqq_portfolio | QLD | 9.979421 | 9.979421 | provider_checked | 1/1 |
| tqqq_portfolio | TQQQ | 618.7715750 | 618.771575 | provider_checked | 3/3 |
| vcmoney_portfolio | APO | 0.656207 | 0.656207 | provider_checked | 0/0 |
| vcmoney_portfolio | BX | 0.678077 | 0.678077 | provider_checked | 0/0 |
| vcmoney_portfolio | CG | 1.973815 | 1.973815 | provider_checked | 0/0 |
| vcmoney_portfolio | KKR | 0.842978 | 0.842978 | provider_checked | 0/0 |
| vcmoney_portfolio | RVII | 35.676120 | 35.67612 | provider_checked | 0/0 |
| vcmoney_portfolio | VCX | 561.755593 | 561.755593 | needs_review | 0/0 |
