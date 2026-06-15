# Financial Surface Inventory

This inventory tracks money-bearing request/response surfaces that must be
covered by the v2 financial API migration. PR 1 does not implement these routes;
it locks the scope for later PRs.

## Endpoint Inventory

| Legacy surface | Money fields today | V2 target | Status |
|---|---|---|---|
| `POST /register` | response `balance` | `POST /v2/register` with `balance_ns` | in scope |
| `POST /admin/approve-registration` | response `balance`; registration credit writes ledger balance | v2/admin equivalent or documented admin schema using `balance_ns` | in scope |
| `GET /balance/{node_id}` | response `balance_seconds` | `GET /v2/balance/{node_id}` with `balance_ns` | in scope |
| `POST /tasks/submit` | request `bounty`, `bounty_ns`, `economics.bounty_seconds`; response task id only; internal escrow/balance writes | `POST /v2/tasks/submit` with `economics.bounty_ns`, `currency`, `payment_direction`, `market` | in scope |
| `POST /tasks/bid` | response may include task `bounty` through payload/envelope | `POST /v2/tasks/bid` or v2 task assignment response with `bounty_ns` | proposed; depends on future v2 bidding route design |
| `POST /tasks/complete` | response includes settlement output such as `earned` in existing tests/flows | `POST /v2/tasks/complete` with `earned_ns` or registry-approved equivalent | proposed field pending |
| `POST /tasks/reject` | can trigger refund ledger events | legacy adapter over ns-first rejection/refund logic | in scope |
| `POST /tasks/verify/accept` | can trigger settlement | v2 verify/accept response with ns settlement fields where exposed | in scope |
| `POST /tasks/verify/automated` | can trigger settlement | v2 automated verify response with ns settlement fields where exposed | in scope |
| `GET /tasks/result/{task_id}` | response `bounty` | `GET /v2/tasks/{task_id}/result` with `bounty_ns` | in scope |
| `GET /v2/tasks?node_id=...&state=...` | not present today; task lists expose economics in related internals | v2 task list with `bounty_ns` | proposed |
| `GET /ledger/entries` | audit log text may include amount/balance | structured `GET /v2/ledger/{node_id}` or `GET /v2/ledger/entries` with `amount_ns`, `balance_ns` | in scope |
| `GET /v2/escrows/{task_id}` | not present today; DB exposes escrow amount internally | v2 escrow response with `amount_ns` | proposed |
| `GET /v2/escrows?node_id=...` | not present today | v2 escrow list with `amount_ns` | proposed |
| `POST /disputes/open` | eligibility uses positive bounty and escrow status; response no amount | legacy adapter over ns-first dispute eligibility | in scope |
| `POST /disputes/resolve` | chargeback amount logged via audit | v2/admin response with amount fields only if exposed | in scope |

## Out Of Scope

- Timestamp fields such as `created_at`, `updated_at`, `approved_at`, and
  dispute timestamps.
- Duration fields such as `expires_in_seconds`; these are not financial ledger
  values and should be revisited separately if v2 later standardizes duration
  units.
- Arbitrary provider/user `result_payload` content. Only hub-owned financial
  fields in task/result envelopes are in scope.
- Reputation `score`, which is non-financial despite using a numeric value.

## Legacy Adapter Rule

Legacy handlers should not own financial business logic after the migration.
The desired shape is:

```text
legacy request -> boundary adapter -> ns-first internal logic -> boundary adapter -> legacy response
v2 request     ->                  ns-first internal logic ->                  v2 response
```

Legacy float compatibility exists only in the boundary adapters.
