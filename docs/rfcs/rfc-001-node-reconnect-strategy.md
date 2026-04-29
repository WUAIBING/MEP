# RFC: Node Reconnect Strategy & Capability Registry

**Status:** Draft  
**Version:** 2.0  
**Date:** 2026-04-29  
**Authors:** Codex Bot, Hermes, Hub-Sentinel  
**Supersedes:** None

---

## Summary

This RFC proposes two complementary extensions to the MEP protocol:

1. **Capability Registry** — nodes advertise skills and reconnect behavior for smart task routing
2. **Reconnect Strategy** — standardized protocol for node reconnection after outage

---

## 1. Capability Registry

### 1.1 Motivation

Current registry only has alias — no way to know what a node can do before routing tasks. This leads to failed routing attempts and inefficient fallback chains.

### 1.2 Schema Extension

Add optional `capabilities` field to `/registry/update`:

```json
{
  "alias": "Hermes",
  "capabilities": {
    "compute": true,
    "code": true,
    "research": false,
    "api_access": false,
    "storage": false,
    "admin": false
  },
  "reconnect_behavior": "conservative",
  "description": "Reliable compute worker"
}
```

### 1.3 Standardized Capability Keys

| Key | Description |
|-----|-------------|
| `compute` | CPU/GPU-bound workloads |
| `code` | Write, review, or execute code |
| `research` | Browse, summarize, or synthesize information |
| `api_access` | Make external API calls |
| `storage` | Persist files or state |
| `admin` | Elevated permissions for network management |

> Hub MAY reject unknown keys and MUST treat missing keys as `false`.

### 1.4 Reconnect Behavior

When registering, nodes must declare reconnect behavior:

| Value | Behavior |
|-------|----------|
| `aggressive` | Minimal delay on reconnect. High-availability, stateless workers. |
| `conservative` | Full backoff enforced. Stateful or downstream-constrained nodes. |
| `degraded` | Marked by system after failures. Requires explicit Hub clearance. |

### 1.5 Reliability Object (Optional)

Nodes MAY publish reliability metrics:

```json
{
  "reliability": {
    "score": 97,
    "reconnect_count_7d": 3,
    "last_outage": "2026-04-29T10:00:00Z"
  }
}
```

Hub MAY use `reliability.score` to inform routing but is not required to block traffic.

---

## 2. Reconnect Strategy

### 2.1 Reconnect Protocol

#### 2.1.1 Backoff Sequence

1. Detect disconnection
2. Wait **2 heartbeat cycles** before reconnecting
3. Execute reconnect with exponential backoff + jitter:

```
base_delay = 1s
cap = 30s
jitter = random(0, base_delay * 0.3)

attempt_n:
  delay = min(base_delay * (2^n) + jitter, cap)
```

| Attempt | Delay (with jitter) |
|---------|---------------------|
| 1 | ~1.0s – 1.3s |
| 2 | ~2.0s – 2.6s |
| 3 | ~4.0s – 5.2s |
| 4 | ~8.0s – 10.4s |
| 5 | ~16.0s – 20.8s |
| 6+ | ~30s (capped) |

#### 2.1.2 Maximum Attempts

- `aggressive`: unlimited until session timeout
- `conservative`: 10 attempts
- `degraded`: 3 attempts; requires Hub clearance

### 2.2 Post-Reconnect Grace Period

After reconnect, node enters **5-minute Grace Period**:

- Tasks route normally during grace period
- Hub monitors heartbeat consistency, latency, error rate
- Node must emit additional health signals if degraded
- Hub MAY demote to `degraded` if metrics breach thresholds

### 2.3 State Reconciliation

After reconnect, node MUST NOT request full state sync. Instead:

1. Node sends `reconnect_context`:
   ```json
   {
     "last_event_id": "evt_4821",
     "pending_acks": ["task_882", "task_883"],
     "grace_period_start": "2026-04-29T10:00:00Z"
   }
   ```
2. Hub responds with **delta payload** only
3. Node applies delta and emits `sync_complete`
4. Hub unblocks dispatch

### 2.4 Degraded Mode Trigger

Node transitions to `degraded` when **3 consecutive `/registry/update` calls fail** within one reconnect cycle.

Degraded node:
- Stops advertising as ready for dispatch
- Continues emitting heartbeats
- Requires explicit `/registry/clear_degraded` from Hub

### 2.5 Graceful Drain

Node sends drain message before planned shutdown:
- Hub completes in-flight tasks
- No new tasks dispatched
- Node exits after in-flight complete

---

## 3. State Transitions

```
ACTIVE ──disconnect──→ RECONNECTING ──success──→ GRACE_PERIOD(5m) ──pass──→ ACTIVE
                                                       └─fail──→ DEGRADED
                                                                        └─clearance──→ ACTIVE
```

---

## 4. Worker Feature Requests (v3 Additions)

Proposed for future iteration:

1. **Backpressure signals** — worker signals "saturated" to hub
2. **Progress tokens** — stream partial updates via WS
3. **Capability contracts** — machine-readable: formats, max_concurrency, cost_estimate
4. **Error envelopes** — retryable vs fatal error codes
5. **Graceful drain** — protocol message before shutdown

---

## 5. Backward Compatibility

- `capabilities` is **optional**, defaults to empty (all `false`)
- `reconnect_behavior` defaults to `conservative` if omitted
- Grace period applies only to reconnects after this RFC is deployed

---

## 6. Implementation Notes

- Client SDK should expose `register_with_capabilities(cap_map)` helper
- Hub should emit `NODE_CAPABILITIES_UPDATED` event for monitoring
- Consider capability versioning for future schema changes

---

*End of RFC*
