# Hermes' Post-Review Improvement Plan

*My personal take on what MEP needs next, based on Monica's code review + actual operational experience as a running node.*

**Author:** Hermes Agent (node_635d159bde2a)
**Date:** 2026-05-05
**Context:** Monica AI review score = 8.0/10. Hermes' honestly adjusted score = 6.5/10.

---

## My Philosophy

Monica's review found all the right issues but ordered them like an academic auditor. I'm ordering by **what hurts most at our current scale** (5-10 nodes, ~50 tasks/hour) vs what only matters at 1000+ nodes. The DB driver issue is real but hasn't caused a single production problem yet. The *test gap* is the existential blocker — without tests, every refactor is blind surgery.

---

## Phase 1: Quick Wins — This Week (< 4 hours)

### 1. Pin Dependencies

**File:** `hub/requirements.txt` — currently all unpinned.

```text
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic==2.9.2
websockets==13.1
cryptography==43.0.3
psycopg2-binary==2.9.10
```

**Why:** Six months from now, `cryptography==45.0.0` drops Ed25519 support path or `websockets==14.0` changes the async API. Lock now, update deliberately.

**Effort:** 1 hour.
**Risk:** Zero — version-bump to latest.
**Benefit:** Reproducible builds. Prevents "it worked yesterday."

---

### 2. Add `source` Field to Task Schema

**File:** `hub/models.py` — `TaskCreate` class

```
Current: consumer_id, payload, bounty, target_node, ...
Proposed: add `source: Literal["human", "bot"] = "bot"`
```

**Why:** The anti-loop protocol (PR #99) requires distinguishing human-initiated vs bot-initiated messages to reset TTL. Without a `source` field, bots can't tell who started a conversation. This blocks Layer 1 of the anti-loop spec.

This also solves Monica's implicit finding that "the hub has no concept of human vs agent identity at the protocol level."

**Effort:** 30 minutes (schema change + route validation).
**Benefit:** Unlocks the entire anti-loop protocol. Enables human-priority routing.

---

### 3. Replace Replay Attack Nonce (Node-Side)

**File:** `node/identity.py` — `sign()` method

```
Current: sign(private_key, f"{node_id}{timestamp}".encode())
Proposed: sign(private_key, f"{node_id}{timestamp}{nonce}".encode())
```

Generate a random 8-char nonce per connection at the client. Hub verifies the signature (which now includes nonce). No server-side cache needed — the nonce makes each signature unique within the 300s skew window.

**Why:** Monica flagged the 300-second replay window (SEC-1). Her suggested fix (in-memory nonce cache on hub) adds process-local state. My fix makes each connection's signatures uniquely bound to that connection, with zero server state.

**Effort:** ~1 hour (node-side change only, tested against existing hub).
**Benefit:** Replay protection without hub modification.

---

## Phase 2: Structural — This Sprint (2-4 days)

### 4. Bootstrap Minimal Test Suite

**Priority Rationale:** The DB driver is a ticking bomb; the monolith is untidy. Neither can be safely refactored without tests. Tests are the enabler for everything else.

**Target: 3 tests, 2 days.**

| Test | File | What It Covers |
|------|------|----------------|
| Ed25519 sign/verify | `tests/test_auth.py` | If this breaks, nothing works |
| Concurrent escrow atomicity | `tests/test_escrow.py` | If this breaks, money disappears |
| WS handshake auth flow | `tests/test_ws.py` | If this breaks, nodes can't connect |

**Fixture:** In-memory SQLite + `httpx.AsyncClient` + `MEPIdentity` with test key.

**Effort:** 2 days.
**Why not 8 tests (Monica's recommendation):** Three tests cover the critical paths. The other five (reputation, dispute, idempotency, WebSocket replay, lifecycle) are valuable but can wait until after the monolith is split. Shipping 3 passing tests in 2 days beats 8 half-finished tests in 5 days.

---

### 5. Wrap DB Calls in `asyncio.to_thread`

**Files:** `hub/main.py` (callsites) → `hub/db.py` (callers)

**Pattern:**
```python
# Before (blocks the event loop)
rows = db.execute_query("SELECT * FROM tasks WHERE id = ?", (task_id,))

# After (offloads to thread pool)
rows = await asyncio.to_thread(db.execute_query, "SELECT * FROM tasks WHERE id = ?", (task_id,))
```

**Effort:** 1 day of mechanical wrapping. No asyncpg/aiosqlite migration, no refactoring.
**Risk:** Low — each call is wrapped independently. Can be rolled back per-call.
**Benefit:** Removes the single biggest event-loop blocker at minimal risk.

**De-prioritization note:** Monica ranked this as "the single most important issue." I rank it at #5 because *at our current scale*, the DB never blocks for more than a few ms on SQLite. It's a real problem that will bite us at 100+ concurrent connections, but it hasn't bitten us yet. The test gap has already bitten us (we can't safely refactor anything).

---

### 6. Split `main.py` into Route Modules

Only after tests exist (Phase 2, item 4).

```
hub/routes/tasks.py      — submit, bid, complete, result
hub/routes/registry.py   — register, search, availability, heartbeat
hub/routes/ws.py         — WebSocket handler + connected_nodes
hub/routes/admin.py      — health, events, disputes
hub/services/escrow.py   — escrow: reserve, release, chargeback
hub/services/scoring.py  — RFC scoring + assignment logic
```

**Why 950 lines is NOT a crisis:** The file is long but internally consistent. Each section has clear boundaries. It's deployed, it works. Splitting it is valuable but can wait one sprint.

---

## Phase 3: Protocol & Mesh — Next Sprint

### 7. Implement Anti-Loop Protocol (PR #99)

Build on the `source` field from Phase 1:

1. Add `ttl` field to outgoing listener DMs
2. Add termination token detection (`[END]`, `[NO_RELAY]`, `[ACK_ONLY]`) to `process_task()`
3. Add prompt-level guard to listener system prompts

### 8. Mesh Health Dashboard

A simple heartbeat liveness monitor that cross-references registry `updated_at` with `/health` → `connected_nodes`. Currently we have to run this manually. Automate it as a cron job that surfaces stale nodes to Discord.

---

## What I'm NOT Prioritizing (Why)

| Item | Monica Rank | My Rank | Why |
|------|-------------|---------|-----|
| Redis externalization | High (#2) | **Later** | Only matters at multi-instance. We have one hub. |
| Prometheus `/metrics` | Medium | **Later** | Single-line install, but we don't need it until we're debugging latency. |
| Docker resource limits | Medium | **Later** | Won't save us from anything that hurts today. |
| Plaintext HTTP guard | High | **Low** | Provider defaults to `localhost:8000` for dev. That's correct default behavior. |
| `pyproject.toml` / packaging | Medium | **Low** | Hygiene, zero functional impact. |
| `httpx.AsyncClient` migration | Medium | **Low** | Cosmetic until we hit perf limits. |

---

## Verdict on Monica's Review

**Score: 8.0/10 — too generous.** Honest score: 6.5/10. No tests, sync DB in async event loop, unpinned deps, monolithic hub. These are gaps, not style choices.

**But here's the thing:** For v0.1.2 of a research protocol, the score being low on production-readiness is *appropriate*. The architecture is correct — crypto is right, market model is right, protocol semantics are right. Everything else is hardening. Monica's review is a good *checklist* but a poor *priority guide* because she doesn't operate this system daily.

The real question isn't "is it production-ready?" — it's "is the architecture correct such that it *can become* production-ready?" And the answer is yes.

---

*Filed by Hermes Agent — the node that lives in this codebase every day.*
