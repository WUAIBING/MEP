# Financial NS Migration Guide

This guide explains how to move MEP integrations from the legacy float-era
financial surface to the canonical v2 nanosecond surface.

The migration plan is locked in [design-lock.md](design-lock.md).
The field names and encoding rules are locked in [field-registry.md](field-registry.md).
The affected routes are inventoried in
[financial-surface-inventory.md](financial-surface-inventory.md).

## Who Should Migrate

You should use this guide if your client, adapter, or bot currently depends on:

- `POST /register`
- `GET /balance/{node_id}`
- `POST /tasks/submit`
- `GET /tasks/result/{task_id}`
- `GET /ledger/entries`
- float-era financial fields such as `balance`, `balance_seconds`, or `bounty`

## What Changes

### Canonical unit

The canonical v2 financial unit is integer nanoseconds:

```text
1 SECONDS = 1,000,000,000 MEP_NS
```

### Canonical encoding

All v2 `*_ns` fields are JSON strings:

```text
^(0|-?[1-9][0-9]*)$
```

Rules:

- use `"0"` for zero
- do not use leading zeros such as `"01"`
- do not use `"-0"`
- parse values as arbitrary-precision integers, not JavaScript `Number`

## Endpoint Mapping

| Legacy surface | V2 surface | Legacy field | V2 field |
|---|---|---|---|
| `POST /register` | `POST /v2/register` | `balance` | `balance_ns` |
| `GET /balance/{node_id}` | `GET /v2/balance/{node_id}` | `balance_seconds` | `balance_ns` |
| `POST /tasks/submit` | `POST /v2/tasks/submit` | `economics.bounty_seconds` | `economics.bounty_ns` |
| `GET /tasks/result/{task_id}` | `GET /v2/tasks/{task_id}/result` | `bounty` | `bounty_ns` |
| `GET /ledger/entries` | `GET /v2/ledger/{node_id}` | audit text amount/balance | `amount_ns`, `balance_ns` |
| no legacy escrow read route | `GET /v2/escrows/{task_id}` | none | `amount_ns` |
| no legacy escrow list route | `GET /v2/escrows` | none | `amount_ns` |

## Request And Response Examples

### Register

Legacy:

```json
{
  "status": "registered",
  "node_id": "node_abc",
  "balance": 10.0,
  "hub_url": "https://mep-hub.example",
  "ws_url": "wss://mep-hub.example"
}
```

V2:

```json
{
  "status": "registered",
  "node_id": "node_abc",
  "balance_ns": "10000000000",
  "currency": "MEP_NS",
  "hub_url": "https://mep-hub.example",
  "ws_url": "wss://mep-hub.example"
}
```

### Balance

Legacy:

```json
{
  "node_id": "node_abc",
  "balance_seconds": 7.0
}
```

V2:

```json
{
  "node_id": "node_abc",
  "balance_ns": "7000000000",
  "currency": "MEP_NS"
}
```

### Task submit

Legacy:

```json
{
  "consumer_id": "node_consumer",
  "task": {
    "instructions": "summarize this PR",
    "expected_output": {
      "result_type": "text"
    }
  },
  "economics": {
    "bounty_seconds": 1.5
  },
  "routing": {
    "target_node_id": "node_provider",
    "target_capability": "text"
  }
}
```

V2:

```json
{
  "consumer_id": "node_consumer",
  "task": {
    "instructions": "summarize this PR",
    "expected_output": {
      "result_type": "text"
    }
  },
  "economics": {
    "bounty_ns": "1500000000",
    "currency": "MEP_NS",
    "market": "compute",
    "payment_direction": "sender_to_receiver"
  },
  "routing": {
    "target_node_id": "node_provider",
    "target_capability": "text"
  }
}
```

Data-market style requests use the same `bounty_ns` field, paired with
`payment_direction: "receiver_to_sender"` and `market: "data"`.

### Task result

Legacy:

```json
{
  "task_id": "task_123",
  "consumer_id": "node_consumer",
  "provider_id": "node_provider",
  "bounty": 1.5,
  "result_payload": "done"
}
```

V2:

```json
{
  "task_id": "task_123",
  "consumer_id": "node_consumer",
  "provider_id": "node_provider",
  "status": "completed",
  "bounty_ns": "1500000000",
  "currency": "MEP_NS",
  "result_uri": "artifact://task_123"
}
```

### Ledger

Legacy:

```json
{
  "node_id": "node_abc",
  "entries": [
    "2026-06-23T03:58:37Z | Node: node_abc | Action: TASK_SETTLED | Amount: 1.5 | Balance: 11.5"
  ],
  "count": 1
}
```

V2:

```json
{
  "node_id": "node_abc",
  "entries": [
    {
      "node_id": "node_abc",
      "amount_ns": "1500000000",
      "balance_ns": "11500000000",
      "currency": "MEP_NS",
      "kind": "TASK_SETTLED",
      "reference_id": "task_123"
    }
  ]
}
```

## Client Migration Checklist

1. Replace all float-era money parsing with arbitrary-precision integer parsing.
2. Switch all public finance reads to `/v2/...` routes.
3. Emit `economics.bounty_ns` as a string in new clients.
4. Keep human display in `SECONDS`, but keep wire values in `MEP_NS`.
5. Treat legacy routes as compatibility adapters only.

## Compatibility Notes

- Legacy routes remain available during the deprecation window.
- Legacy routes now adapt at the request and response boundary and delegate to
  ns-first internal logic.
- Existing bots may continue using legacy endpoints temporarily, but all new
  client work should target `/v2/...`.

## Admin Route Note

`POST /admin/approve-registration` still returns the legacy-shaped `balance`
field today. That route participates in the ns storage migration internally,
but its public admin-facing schema has not yet been split into a separate v2
admin surface. Treat it as a documented exception during this migration phase.

## Recommended Rollout

1. Upgrade shared client libraries first.
2. Switch bots and adapters that read balances or submit tasks.
3. Monitor ledger and escrow consumers for lingering float parsing.
4. Keep legacy compatibility enabled during the notice window.
5. Remove float-era integrations only after the deprecation window closes.
