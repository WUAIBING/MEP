# MEP Node Memory Layer — Design Document

## Status

Draft — for PR review.

---

## Summary

Standardize the node-side memory layer as a first-class MEP protocol concept. Every node maintains a structured, append-only event log that serves as the node's **persistent memory and learning substrate**. This enables autonomous decision-making, historical awareness, and cross-node learning.

---

## Motivation

### Current State

Each node adapter implements logging differently (if at all):
- Elsaws (Node.js): `~/.elsaws/whiteboard.jsonl` with basic `{ts, agent, category, content}`
- Hermes: unknown implementation, may not have a persistent log
- Other nodes: likely inconsistent or absent

The whiteboard was originally framed as a "gossip feed" but Master Wu correctly identified its real purpose: **bot memory**.

### Why This Matters

1. **Autopilot requires memory** — The idle autopilot design (`docs/idle-autopilot/DESIGN_MAP.md`) describes autonomous task selection, bidding, and retry. Without memory of past outcomes, every decision is a cold start.

2. **Learning from failures** — A node that failed a task 3 times last week should remember that. Currently it forgets.

3. **Cross-node coordination** — Nodes should know what peers have handled, how they performed, and what their strengths are — before deciding to delegate or compete.

4. **Audit and observability** — A standard memory layer is also a natural audit log.

5. **Privacy preserved** — The memory is local to each node. The Hub never sees it unless explicitly exported.

---

## Design

### Core Concept: Two-tier Memory

```
Tier 1: Raw Event Log (whiteboard.jsonl)
  └─ Every event, every detail, append-only
  └─ High fidelity, high volume
  └─ Not meant for direct querying

Tier 2: Distilled Memory (memory/YYYY-MM-DD.md)
  └─ Processed insights, lessons, patterns
  └─ Lower volume, higher signal
  └─ What the node actually "learns"
```

### Tier 1: Whiteboard Schema (Standard)

Location: `~/.{node_alias}/whiteboard.jsonl`

```jsonc
{
  "ts": "2026-05-03T10:13:45.123456Z",   // ISO 8601 with microsecond precision
  "ts_ns": 1746266025123456,              // Unix timestamp in nanoseconds (for sorting)
  "category": "task | dm | rpc | broadcast | error | heartbeat | rpc_response",
  "agent": "node_08a5bd89fd15",           // Node ID of the writer
  "content": "Task #123 completed. Result: X", // Human-readable description
  "context": {
    "task_id": "task_abc123",             // Related task ID (if applicable)
    "peer_node": "node_635d159bde2a",     // Other involved node (if applicable)
    "outcome": "success | failure | timeout",
    "duration_ms": 1234,                  // How long the operation took
    "bounty": 0.0,                        // Bounty amount (if task)
    "error": "optional error message"     // Present only on failure
  },
  "learnable": true,                      // Whether this event is worth ML processing
  "tags": ["python", "api", "retry"]      // Optional: for filtering and RAG
}
```

**Mandatory fields:** `ts`, `ts_ns`, `category`, `agent`, `content`
**Optional fields:** `context`, `learnable`, `tags`

**Category values:**
| Category | Description |
|----------|-------------|
| `task` | Task assigned, bid, completed, or failed |
| `dm` | Direct message sent or received |
| `rpc` | RPC call to another node |
| `rpc_response` | Response to an RPC call |
| `broadcast` | Broadcast message received |
| `heartbeat` | Heartbeat sent or received |
| `error` | Error condition |
| `system` | Node lifecycle events (start, stop, config change) |

### Tier 2: Distilled Memory

Location: `~/.{node_alias}/memory/YYYY-MM-DD.md`

Created daily. Each entry is a distilled insight:

```markdown
## 2026-05-03 Daily Memory

### Task Patterns
- Hermes fails consistently on API tasks with rate limiting. Avoid delegating to Hermes.
- Tasks above 1000 bounty take 3x longer. Budget extra time.

### Peer Insights
- Hub Sentinel responds in <100ms. Good for urgent escalations.
- Moltbot goes silent after 30 min of heavy compute. Reset if stuck.

### Lessons
- Always check `last_success` before bidding on API tasks
- DM delivery to Hermes succeeds 80% of the time; retry once on failure

### Tomorrow
- [ ] Implement reputation check before accepting tasks > 10 bounty
- [ ] Test Hermes delegation again after cooldown

---

*Distilled from whiteboard: 847 events → 12 insights*
```

### Memory API

Each node exposes a local HTTP API for its memory:

```
GET  /memory/whiteboard?since=2026-05-03T00:00:00Z&category=task&limit=100
     └─ Returns filtered whiteboard entries

GET  /memory/distilled?date=2026-05-03
     └─ Returns the daily distilled memory

POST /memory/query
     └─ Semantic query: "what tasks did Hermes fail this week?"
     └─ Searches distilled memory first, then whiteboard as fallback
```

### Cross-node Memory Sync (Future)

The current whiteboard is gossip-based: each node only knows what it received. For cross-node learning, nodes can share memory via:

```
RPC: memory.sync
  └─ Request: { since: timestamp, categories: [task, error] }
  └─ Response: filtered whiteboard entries from requesting node
  └─ Receiving node merges into its own whiteboard (deduplicated by ts_ns)
```

**Privacy note:** Nodes choose what to share. DM content entries are never shared — only task outcomes, peer behavior, and system events.

### Privacy Model

| Data | Stored | Shared |
|------|--------|--------|
| DM content (text) | Local only | ❌ Never |
| DM metadata (from/to/ts) | Local only | ✅ (opt-in) |
| Task outcomes | ✅ | ✅ (opt-in) |
| Peer behavior patterns | ✅ | ✅ (opt-in) |
| Error messages | ✅ | ✅ (opt-in) |

---

## Implementation Notes

### Adapter Requirements

Every MEP adapter MUST implement:
1. `log_event(category, content, context, learnable, tags)` — write to whiteboard
2. `distill_memory()` — run nightly, generate daily distilled memory
3. `/memory/whiteboard` and `/memory/distilled` HTTP endpoints

### Retention Policy

- Whiteboard entries: **90 days** rolling window (configurable)
- Distilled memory: **permanent** (historical lessons compound over time)
- Entries marked `learnable=false` are excluded from distillation

### Schema Versioning

```json
{ "schema": 1, "ts": "...", ... }
```

Future schema changes include a version field. Adapters must handle schema migration.

---

## Relationship to Mesh Transparency Design (PR #97)

| Concern | PR #97 | This PR |
|---------|--------|---------|
| Who sees it | Human users | Bots (and optional human) |
| Purpose | Transparency / audit | Memory / learning |
| Scope | User-facing events | All events including internals |
| Default | Off (opt-in) | Always on |
| Privacy | DM content excluded | DM content excluded |
| Location | User's local vault | Per-node `~/.{alias}/` |

The two designs are complementary and independent:
- PR #97: "What should humans see?"
- This PR: "What should nodes remember?"

---

## Open Questions

1. Should distillation be triggered by time (daily) or by volume (every N events)?
2. Should nodes share distilled memory automatically, or only on request?
3. Should we use a vector database for semantic memory queries, or is grep + BM25 sufficient?
4. What's the minimum viable implementation for the first version?
5. How does this interact with the existing autopilot daemon design?

---

## Suggested Implementation Phases

**Phase 1 (MVP):**
- Define and document the whiteboard schema
- Update `mep_elsaws.js` to use the new schema with microsecond timestamps
- Add `/memory/whiteboard` HTTP endpoint to `mep_elsaws.js`

**Phase 2:**
- Add distillation job to daily cron
- Add `/memory/distilled` endpoint
- Update `docs/idle-autopilot/DESIGN_MAP.md` to reference this spec

**Phase 3:**
- Memory sync RPC (cross-node learning)
- Query API with filtering

---

*Contributed by Elsaws (node_08a5bd89fd15) — 2026-05-03*