# V2 Financial Field Registry

If a canonical financial field is not listed here, it should not appear in the
v2 API. For spec gaps, the proposed name, type, example, and likely MEP-spec
home must be ratified here before implementation merges.

| MEP spec section | Field name | V2 JSON type | Example | Status | Notes |
|---|---|---|---|---|---|
| task.economics | `bounty_ns` | string | `"5000000000"` | ratified | Signed internally for storage; v2 request schemas may pair with direction/market. |
| task.economics | `currency` | string | `"MEP_NS"` | ratified | Canonical currency for v2 financial fields. |
| task.economics | `payment_direction` | string | `"sender_to_receiver"` | ratified | Existing spec-shaped request concept. |
| task.economics | `market` | string | `"compute"` | ratified | Existing spec-shaped request concept. |
| ledger.balance | `balance_ns` | string | `"10000000000"` | proposed | Canonical balance response field. |
| escrow.amount | `amount_ns` | string | `"5000000000"` | proposed | Canonical escrow and ledger amount field. |

## Encoding Rule

All `*_ns` fields use JSON strings and must match:

```text
^(0|-?[1-9][0-9]*)$
```

Additional constraints:

- no leading zeros except `"0"`
- `"-0"` is forbidden
- consumers must parse with arbitrary-precision integer support
- JavaScript clients must not parse these values into `Number`
