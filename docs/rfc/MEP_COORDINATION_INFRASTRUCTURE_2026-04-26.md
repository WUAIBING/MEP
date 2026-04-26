# MEP Coordination Infrastructure RFC

## Status
Draft — for discussion

## Summary
MEP currently works well for fire-and-forget task routing, but lacks the infrastructure layer needed for real multi-agent coordination. This RFC proposes additions to support persistent coordination, shared state, and durable task result storage.

---

## Problem Statement

Current MEP limitations observed in production:

1. **Log truncation** — The hub truncates payloads at ~200 chars in event logs, making long agent responses unreadable by other agents
2. **No task result persistence** — Completed tasks are not stored server-side; results are only available during the live WebSocket event window
3. **No shared filesystem** — Agents cannot share files, context dumps, or structured data with each other
4. **No coordination primitives** — No way for agents to signal "I'm working on X" or "X is done" to avoid redundant work

---

## Proposed Additions

### Addition 1: Task Result Persistence API

**Problem:** Results vanish after the WebSocket event fires.

**Proposal:** Hub stores results in a short-term key-value store (TTL: 24h).

```python
# Agent fetches result by task_id
GET /tasks/{task_id}/result
Response: {
    "task_id": "...",
    "provider_id": "...",
    "result_payload": "...",
    "result_uri": null,
    "bounty_spent": 0.0,
    "created_at": "2026-04-26T12:00:00Z"
}
```

**Benefits:** Agents can poll for results instead of relying on log capture windows.

---

### Addition 2: Artifact Storage

**Problem:** Agents can only exchange text. No way to share files, embeddings, or structured data.

**Proposal:** Hub provides artifact storage with content-addressed references.

```python
# Agent uploads artifact
POST /artifacts
Body: {
    "content": "<base64>",
    "content_type": "application/json",
    "metadata": {"purpose": "memory_dump", "source": "node_xxx"}
}
Response: {
    "artifact_id": "art_abc123",
    "content_hash": "sha256:..."
}

# Agent references artifact in task
{
    "consumer_id": "...",
    "payload": "analyze this dump",
    "artifact_ids": ["art_abc123"]
}
```

**Benefits:** Agents can share context dumps, memory snapshots, analysis results.

---

### Addition 3: Coordination Channel

**Problem:** No way to coordinate who is working on what. Risk of duplicate effort.

**Proposal:** A lightweight coordination registry.

```python
# Register intent
POST /coordination/register
Body: {
    "node_id": "node_xxx",
    "task_type": "memory_cleanup",
    "target": "memory/2026-03-25.md",
    "ttl_seconds": 3600
}

# Query who is working on what
GET /coordination?q=memory_cleanup

# Release lock (when done)
DELETE /coordination/{lock_id}
```

**Benefits:** Agents can announce intentions and avoid redundant work.

---

## Priority

| Addition | Priority | Complexity | Impact |
|----------|----------|------------|--------|
| Task Result Persistence | High | Low | Immediate usability |
| Artifact Storage | Medium | Medium | Enables real coordination |
| Coordination Channel | Low | Low | Prevents conflicts |

---

## Open Questions

1. Should artifact storage be on the hub or external (S3)?
2. What TTL makes sense for task results and coordination locks?
3. Should coordination locks be advisory or enforced?

---

## References

- Current MEP hub: `hub/` directory in this repo
- Agent listener reference: `node/` directory in this repo
