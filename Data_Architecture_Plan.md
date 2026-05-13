# Data Architecture Plan

## Context

The app currently mixes code, source transaction ledgers, and generated runtime data in the repository. This has created friction when staging and main diverge, and when generated files change locally as the app refreshes portfolio data.

We are not implementing this migration now. This document captures the future direction.

## Recommendation

Move portfolio source data to a database and keep generated/cache data out of git.

Keep in git:
- Application code
- Tests
- Documentation
- Optional seed/demo data

Move out of git:
- Portfolio transactions
- User-owned portfolios
- Generated portfolio/cache files
- Price history cache
- Daily value cache
- Refresh/freshness metadata

## Design Goals

1. Page load should remain fast by loading cached data first.
2. The app should load cached snapshots for all portfolios up front so switching portfolios is instant.
3. If data is stale, refresh should run automatically in the background.
4. Eventually support multiple users.
5. Eventually support user CRUD for transactions: add, edit, delete.
6. Avoid row-count based syncing; every transaction should have a stable identity.

## Proposed Data Model

Core tables:

- `users`
- `portfolios`
- `transactions`
- `portfolio_snapshots`
- `holding_snapshots`
- `daily_values`
- `price_cache`
- `refresh_jobs`

Example `transactions` fields:

```text
id
portfolio_id
date
ticker
purchase_price
shares_purchased
source
created_at
updated_at
deleted_at
```

Example `portfolio_snapshots` fields:

```text
portfolio_id
as_of
portfolio_value
invested
dividends
gain_loss
freshness_status
```

Example `daily_values` fields:

```text
portfolio_id
date
main_value
voo_value
qqq_value
```

## Refresh Flow

On page load:

1. Query cached snapshots for all portfolios.
2. Render immediately from cached data.
3. Determine which portfolios are stale.
4. Trigger refresh for stale portfolios in the background.
5. UI shows freshness and refresh status.
6. When refresh completes, reload the page or fetch updated snapshots through an API.

Generated/cache data should be environment-local. Staging and production can safely have separate computed caches as long as they derive from the same transaction source.

## Multi-User Direction

Add ownership and access control around portfolios:

- A user can own one or more portfolios.
- A portfolio belongs to a user or shared account/group.
- Transactions are tied to portfolio IDs, not filesystem paths.
- CRUD actions update transaction rows and mark affected portfolios dirty.

## Phased Migration

1. Stop tracking generated data files.
   - Remove tracked `last_updated.txt`, `daily_values.csv.meta`, and any generated CSVs from git.
   - Keep only source files needed for the current CSV workflow.

2. Make transaction sync idempotent.
   - Replace row-count based sync with stable transaction IDs or content hashes.
   - This prevents corruption when older transactions are inserted.

3. Introduce a database.
   - Prefer Postgres for Render production/staging.
   - SQLite is acceptable for local development only.

4. Move transactions into database tables.
   - Import existing CSV transaction ledgers.
   - Keep CSV export/import as an admin or backup workflow if useful.

5. Store snapshots and daily values in database tables.
   - Load all portfolio cached summaries in one request.
   - Keep the UI fast even before refresh completes.

6. Add transaction CRUD.
   - Add, edit, and delete transactions through the app.
   - Mark portfolios dirty and trigger recomputation.

7. Add background refresh jobs if needed.
   - Start with web-triggered refresh.
   - Add a worker/cron only if reliability or latency requires it.

## Render Cost Estimate

Current relevant Render pricing assumptions:

- Web service Starter: about `$7/month` each.
- Persistent disks: about `$0.25/GB/month`.
- Render Postgres:
  - Free: `$0`, but time-limited and not suitable for production persistence.
  - Basic-256mb: about `$6/month` plus storage.
  - Basic-1gb: about `$19/month` plus storage.
  - Postgres storage: about `$0.30/GB/month`.

For this app, database size should be very small.

Estimated added cost for one production Postgres database:

```text
Basic-256mb Postgres: about $6/month
Storage: about $1.50/month if 5 GB minimum
Total added: about $7.50/month
```

Estimated added cost for separate staging and production databases:

```text
About $15/month added
```

Potential future worker cost:

```text
Background worker on Starter: about $7/month
```

Recommendation:

- Use one production Postgres database when this migration starts.
- Use a separate staging database if we want strong isolation.
- Do not add a background worker until refresh reliability or latency requires it.

## Key Tradeoff

Sharing source transaction data across environments reduces branch/data drift. Sharing generated runtime cache across environments is not recommended, because staging should be free to test new computation logic without risking production cache integrity.
