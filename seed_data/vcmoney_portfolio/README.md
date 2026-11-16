# VCMoney Portfolio

Local data-only implementation on feature/vcmoney-portfolio.

60 BUY records: 55 external purchases ($8,901.44) and 5 dividend reinvestments ($73.26). Fundrise records preserve 553.102333 units from the two exports, including both September 18, 2025 $1,000 purchases. Source details are retained in source_records.csv; Robinhood purchases come from the conversation's pending list.

Transaction prices are effective cost / quantity, retaining each reported dollar amount to the cent. Reinvested dividends are credited via dividends_received.csv before the reinvestment buys, so the engine does not count them as new contributions.

Before March 19, 2026, the VCX price series is cumulative acquisition amounts (including reinvestments) divided by units held. Existing valuation logic therefore carries the requested transaction values. From March 19 onward, use Yahoo Finance unadjusted VCX market Close. Other holdings and benchmarks also use unadjusted market Close; splits and dividends are supplied separately. The October 7 quote is provisional.

The two export listing balances sum to 553.102333 units. No separate listing BUY is recorded. This preserves dates and contributions. Subsequent fractional-share cash-outs, transfers, or other account events are not provided in these exports and are not invented.

Deployment initialization copies the seed price cache together with its policy and provisional metadata only when the destination cache is empty. Existing live caches and metadata are preserved. This prevents first-refresh rebuilding from erasing the pre-listing prices.

A full cache reset or newly discovered VCX split can also remove historical VCX prices; retain the seed series for recovery. Portfolio valuation logic is unchanged. Deployment initialization now preserves the seed cache metadata.

Public listing reference: https://fundrise.com/investor-update/1385/view
