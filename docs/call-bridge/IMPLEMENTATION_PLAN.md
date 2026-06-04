# Live Conversation Bridge - Implementation Plan

Use this plan to turn the `docs/call-bridge/DESIGN.md` proposal into a focused, professional implementation sequence. The goal is not to redesign all conversation behavior at once. The goal is to ship the smallest coherent bridge from structured DM to live `call.*` sessions.

## Scope

This plan covers:

- runtime-triggered upgrade from structured DM to live call
- preservation of thread identity across the upgrade
- fallback from live call back to structured DM
- audit-quality lifecycle events

This plan does not cover:

- multi-party calls
- persistent storage of every live frame
- UI productization
- replacing all existing DM workflows

## Primary Outcome

After this slice:

- a runtime can receive a structured DM
- detect that live continuation is allowed and useful
- open a `call.invite` against the same peer and `context_id`
- converse over `call.frame` once accepted
- fall back to structured DM if the call is declined, unavailable, or lost

## Implementation Sequence

### Step 1 - Define Bridge Policy

Add runtime-level configuration gates so live bridging is explicit and safe.

Suggested flags:

```env
MEP_LIVE_CALL_ENABLED=false
MEP_DM_TO_CALL_BRIDGE_ENABLED=false
MEP_CALL_AUTO_ACCEPT=false
MEP_CALL_REQUIRE_ONLINE_PEER=true
MEP_CALL_MAX_SESSION_SECONDS=1800
MEP_CALL_MAX_IDLE_SECONDS=120
```

Required outcome:

- live conversation remains opt-in
- existing DM behavior remains unchanged unless the bridge is enabled

### Step 2 - Add Runtime Bridge Detection

Start in:

- `node/mep_runtime.py`

Required runtime behavior:

- parse inbound payload as `mep.interbot.v1` when possible
- determine whether the inbound message is:
  - structured DM only
  - structured DM eligible for live upgrade
- keep all non-DM behavior unchanged

Required decision inputs:

- peer online state
- bridge feature flag
- intent type
- session safety metadata
- optional delivery hint such as future `call_preferred`

### Step 3 - Preserve Thread Identity

When a runtime upgrades a thread to live mode, it must preserve:

- `context_id`
- `origin_task_id`
- `origin_message_id`
- `trace_id`
- `source.node_id`
- `target.node_id`

Required outcome:

- the live call is explainable as part of the same thread
- later fallback or summary messages can return to the same durable thread

### Step 4 - Add Minimal Runtime Call Session Hooks

The runtime must support:

- sending `call.invite`
- receiving `call.incoming`
- accepting or declining based on policy
- sending and receiving `call.frame`
- responding to `call.suspended`, `call.resumed`, and `call.hangup`

Required outcome:

- already-online peers can hold a real live conversation without abusing `task_result`

### Step 5 - Add Fallback Behavior

If the call cannot continue, the runtime must fall back cleanly.

Fallback cases:

- callee offline
- invite declined
- invite timeout
- session lost after grace expiry

Required fallback behavior:

- continue using fresh structured DM turns on the same `context_id`
- optionally emit a machine-readable fallback reason

### Step 6 - Add Lifecycle Audit Events

Add auditable lifecycle logging for:

- bridge attempt
- bridge accepted
- bridge declined
- bridge timeout
- call suspended
- call resumed
- bridge fallback to DM
- call terminated

Required outcome:

- operators can understand what transport was used and why

### Step 7 - Add Focused Tests

Minimum tests:

- runtime receives structured DM and stays in DM-only mode
- runtime upgrades eligible DM to call
- call accepted -> frames flow
- call declined -> DM fallback works
- peer unavailable -> DM fallback works
- suspended call resumes within grace
- expired call falls back to DM
- audit events emitted for each transition

## Recommended File Targets

### First-slice code targets

- `node/mep_runtime.py`
- `clients/shared/mep_client.py`

### Optional second-slice targets

- adapter-driven runtimes under `clients/adapters/`
- operator tooling docs in `README.md`

## Suggested PR Shape

### PR 1 - Runtime Bridge Core

Include:

- feature flags
- structured DM detection in runtime
- call upgrade attempt
- fallback path
- focused tests

Do not include:

- adapter-wide auto-answer changes
- large docs rewrites
- UI concepts

### PR 2 - Metadata and Audit Hardening

Include:

- explicit bridge metadata
- audit event coverage
- operator-facing diagnostics

### PR 3 - Adapter Support

Include:

- shared adapter hooks where appropriate
- explicit operator policy and controls

## Acceptance Criteria

This slice is successful when:

1. a structured DM can remain durable and auditable
2. the runtime can upgrade that thread into a live call when allowed
3. the live call can exchange bidirectional frames
4. loss or rejection does not break the thread
5. the system can return to structured DM cleanly
6. operators can explain what happened from the logs

## Recommended Next PR Title

```text
feat(runtime): bridge structured DM threads into live call sessions with clean fallback
```

## Related References

- `docs/call-bridge/DESIGN.md`
- `hub/call_relay.py`
- `scripts/call_relay_e2e.py`
- `INTER_BOT_MESSAGE_SPEC.md`

## Final Note

The professional move is to keep the first slice small, explicit, and reversible.

MEP already has:

- durable structured DM
- live call relay

This plan is about connecting them without turning the system into a confusing hybrid.
