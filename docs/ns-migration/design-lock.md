# Financial NS Migration Design Lock

This document locks the implementation direction from issues #223 and #224.
PR 1 is a foundation PR only: it defines the contract, helper layer, and
inventory. It does not migrate storage, rewrite endpoints, or remove legacy
compatibility.

## Locked Decisions

1. **Versioning**
   - Use path versioning for the new financial API surface: `/v2/...`.
   - Keep legacy endpoints unchanged during the migration window.

2. **Canonical unit**
   - V2 financial request/response schemas use integer nanoseconds only.
   - V2 uses MEP-spec field names wherever practical.
   - No float-backed financial field is part of the canonical v2 API.

3. **JSON encoding**
   - V2 `*_ns` values are JSON strings, not JSON numbers.
   - Encoding rule: values must match `^(0|-?[1-9][0-9]*)$`.
   - No leading zeros except the single value `"0"`.
   - `"-0"` is forbidden; use `"0"`.
   - Client libraries must parse these values as arbitrary-precision integers,
     not JavaScript `Number`.

4. **Storage model**
   - Internal financial storage migrates to integer nanoseconds.
   - Task bounty storage uses signed `bounty_ns` semantics for the first storage
     migration because current hub behavior relies on positive/zero/negative
     bounty branching.
   - Protocol-level unsigned `bounty_ns` plus `payment_direction` / `market`
     can be layered later as a separate protocol upgrade.

5. **Scope**
   - In scope: `ledger.balance`, `tasks.bounty`, `escrows.amount`, and
     registration credit in `approve_registration()`.
   - Out of scope: timestamp columns such as `created_at`, `updated_at`, and
     `approved_at`.

6. **Legacy compatibility**
   - Legacy financial endpoints stay behaviorally compatible during the
     migration window.
   - Legacy endpoints must become thin adapters over canonical ns-first
     helpers/internal logic.
   - Float translation is permitted only at legacy request/response boundaries.

7. **Deprecation window**
   - Default target: 3 months after v2 is announced.
   - The window can be extended if adoption is materially lower than expected
     near the end of the window.

## Backfill Rules

Do not use `int(float_value * 1_000_000_000)` as migration truth.

Backfill must use:

- `Decimal(str(value))`
- an explicit rounding policy
- audit logging for every non-exact legacy row

Cleanup should happen only after mismatches are reviewed.

## PR Sequencing

1. **PR 1:** design-lock artifacts, field registry, endpoint inventory,
   canonical ns helper layer, v2 schema definitions, and helper tests.
2. **PR 2:** internal storage/arithmetic migration to integer ns.
3. **PR 3:** v2 financial endpoints and v2 request handling.
4. **PR 4:** legacy adapter layer plus regression coverage.
5. **PR 5:** docs, migration guide, and deprecation notice.
6. **PR 6:** legacy float-era financial surface removal after the deprecation
   window closes.

## PR 1 Scope

PR 1 should include:

- financial surface inventory document
- canonical field registry artifact
- canonical v2 financial schema definitions
- canonical ns-first helper functions
- legacy-boundary conversion helpers clearly marked as adapters
- tests for ns string encoding and helper behavior

PR 1 should not include:

- full storage migration
- broad endpoint rewrites
- final legacy adapter implementation across routes
- deprecation/removal changes
- cleanup of old float-era endpoints

## PR 1 Acceptance Criteria

- Every money-bearing endpoint is inventoried or explicitly marked out of scope.
- V2 schema definitions use ns-only units.
- V2 schema definitions use MEP-spec names where already defined.
- Unresolved field names are explicitly proposed and tracked in the field
  registry.
- Canonical money helpers establish ns-first internal handling.
- Legacy compatibility is described as boundary adaptation only, not separate
  business logic.
- No new canonical financial schema, internal money helper, or internal
  arithmetic path introduced in PR 1 may use `float` or `REAL` as the source of
  truth.
- Any float-based conversion introduced in PR 1 is explicitly limited to legacy
  boundary adaptation.
- PR 1 introduces no broad API behavior change.
