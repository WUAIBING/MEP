# Financial Legacy Surface Deprecation Notice

This notice starts the migration window for the legacy float-era financial API
surface described in [design-lock.md](design-lock.md).

## Summary

MEP now has a canonical `/v2/...` financial surface that uses nanosecond string
fields only. The older float-era financial surface remains available for a
limited migration window as a compatibility adapter.

New integrations should target the v2 routes immediately.

## Deprecation Window

- target window: 3 months from the public v2 announcement
- window owner: MEP maintainers
- extension policy: the window may be extended if adoption remains materially
  below expectation near the planned end date

This document is the repository notice for that migration plan. If maintainers
publish a release note or operator announcement with a later explicit date,
that dated announcement becomes the operational countdown anchor.

## Affected Legacy Surfaces

The following float-era surfaces are deprecated in favor of the v2 financial
surface:

- `POST /register`
- `GET /balance/{node_id}`
- `POST /tasks/submit`
- `GET /tasks/result/{task_id}`
- `GET /ledger/entries`
- float-era money fields emitted by those routes, including `balance`,
  `balance_seconds`, and `bounty`

## Replacement Surfaces

Use these routes for all new work:

- `POST /v2/register`
- `GET /v2/balance/{node_id}`
- `POST /v2/tasks/submit`
- `GET /v2/tasks/{task_id}/result`
- `GET /v2/ledger/{node_id}`
- `GET /v2/escrows/{task_id}`
- `GET /v2/escrows`

See [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) for endpoint mapping and examples.

## Required Client Behavior

All new or updated clients should:

- send `*_ns` values as JSON strings
- parse `*_ns` values as arbitrary-precision integers
- use `currency: "MEP_NS"` for v2 financial fields
- keep human-facing display in `SECONDS`
- stop treating legacy float values as the source of truth

## Compatibility Promise During The Window

During the migration window:

- legacy routes remain behaviorally compatible
- legacy routes remain boundary adapters only
- internal financial logic remains ns-first
- float translation is allowed only at legacy request and response boundaries

## What Is Not Deprecated By This Notice

- non-financial timestamp fields such as `created_at` and `updated_at`
- duration fields such as `expires_in_seconds`
- arbitrary task `result_payload` content
- admin-only operational flows that still expose legacy-shaped finance fields

## Maintainer Exit Criteria For Removal

The legacy float-era financial surface can move to removal work only after:

1. the deprecation window has elapsed or been explicitly shortened by a future
   design decision
2. shared clients and first-party adapters have moved to `/v2/...`
3. downstream users have had a migration window with documentation and notice

That removal work belongs to the planned `PR 6` step, not this PR.
