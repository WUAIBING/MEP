# PR 6 Legacy Financial Surface Removal Plan

This document is a draft planning artifact for the final finance migration step
defined in [design-lock.md](design-lock.md). It is not the deprecation notice
itself and it does not authorize early removal before the migration window
closes.

## Purpose

`PR 6` is the planned removal step for the legacy float-era financial API
surface after the deprecation window described in
[DEPRECATION_NOTICE.md](DEPRECATION_NOTICE.md).

This draft exists to:

- make the final removal scope visible early
- give integrators a concrete checklist before the cutoff
- let maintainers audit remaining legacy callers before code deletion
- avoid rushing the cleanup work after the window closes

## Timing Gate

The current design lock says the default deprecation window is 3 months after
v2 is announced.

If the repository notice in `PR #235` is used as the announcement anchor, the
earliest eligible merge time for `PR 6` is:

```text
2026-09-23T08:31:19Z
```

This date is provisional. If maintainers later publish a separate release note
or operator announcement with a different explicit anchor date, that later date
becomes the operational countdown anchor instead.

## Planned Removal Scope

The following legacy public routes are expected to be removed in `PR 6`:

- `POST /register`
- `GET /balance/{node_id}`
- `POST /tasks/submit`
- `GET /tasks/result/{task_id}`
- `GET /ledger/entries`

The v2 replacements remain:

- `POST /v2/register`
- `GET /v2/balance/{node_id}`
- `POST /v2/tasks/submit`
- `GET /v2/tasks/{task_id}/result`
- `GET /v2/ledger/{node_id}`
- `GET /v2/escrows/{task_id}`
- `GET /v2/escrows`

## Planned Code Cleanup

`PR 6` is also expected to remove or simplify legacy-only adaptation paths:

- legacy float response conversion that only exists for deleted public routes
- legacy request conversion that only exists to keep float-era submits alive
- legacy-only regression tests that duplicate final v2 coverage

The cleanup should be conservative. If a helper is still needed for backfill,
admin compatibility, or internal data cleanup, it should stay until that
separate dependency is removed.

## Out Of Scope For PR 6

The following should not be removed blindly in the first cleanup pass:

- `POST /admin/approve-registration`, which is still documented as a temporary
  admin exception in [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)
- storage backfill and audit helpers that are still needed for mismatch review
- non-financial timestamp or duration fields
- arbitrary task payload content

If maintainers want the admin route fully aligned first, that should land as a
separate preparatory step before or alongside the actual `PR 6` removal merge.

## Readiness Checklist

- [ ] Confirm the deprecation window anchor date.
- [ ] Confirm the earliest allowed merge date has passed.
- [ ] Audit first-party clients and adapters for legacy route usage.
- [ ] Audit operator docs and examples for legacy finance routes.
- [ ] Decide whether `POST /admin/approve-registration` needs a v2 admin
      replacement before final cleanup.
- [ ] Confirm downstream users have had documentation and notice.
- [ ] Remove the legacy public routes listed above.
- [ ] Remove legacy-only request and response boundary translation where no
      supported caller remains.
- [ ] Remove or rewrite legacy-only regression tests into final v2 coverage.
- [ ] Update docs to state that the legacy financial surface is removed.

## Acceptance Criteria

`PR 6` is ready to merge only when:

1. the deprecation window has elapsed under the chosen anchor date
2. no supported first-party caller still depends on the deleted legacy routes
3. public docs point callers to `/v2/...` only
4. remaining tests validate the v2 surface without depending on float-era
   compatibility

## Notes For Reviewers

This draft PR is intentionally a planning and notice-coordination artifact only.
It should remain in draft state until maintainers decide the cutoff date is
close enough to justify starting code removal work.
