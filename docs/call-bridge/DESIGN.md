# MEP Live Conversation Bridge - Design Document

## Status

Draft - proposed architecture for professionalizing MEP live bot conversation.

---

## Goal

Define a clean, production-oriented bridge between:

- `mep.interbot.v1` structured DM and task semantics
- the new `call.*` real-time relay lane in the hub

The goal is to let MEP support both:

1. durable, auditable, task-oriented inter-bot messaging
2. low-latency, phone-call-style live conversation for already-online peers

This design makes the system easier to explain, easier to operate, and more professional as a product and protocol.

---

## Problem Statement

MEP currently has two partially overlapping conversation models:

- **Structured DM / task path**
  - targeted task submission
  - durable task identity
  - auditability and settlement
  - supports offline delivery and queued delivery
- **`call.*` relay path**
  - real-time WebSocket relay between connected peers
  - low-latency bidirectional frames
  - suspend/resume and peer liveness
  - no task row on the hot path

Before the new relay landed, "phone-call" style bot discussion was not truly realized. Bots could exchange targeted DMs, but the runtime usually answered through `task_result`, not through a fresh live turn.

Now MEP has a real live lane, but the system still lacks a professional bridge between:

- structured intent and governance
- live conversation transport

Without that bridge, MEP risks looking like two separate systems rather than one coherent protocol.

---

## Desired Product Outcome

MEP should present one clear professional story:

- **Use structured DM when you need durable intent, auditability, settlement, or offline tolerance.**
- **Use `call.*` when both peers are online and the interaction should feel live, interactive, and session-oriented.**
- **Use an explicit bridge when a structured DM should escalate into a live session.**

This keeps MEP understandable for operators, developers, and future contributors.

---

## Design Principles

1. One protocol family, two transport modes.
2. Durable intent first, live transport second.
3. Clear upgrade path from DM to call.
4. Safe-by-default behavior; no surprising auto-answer.
5. Explicit operator and runtime policy.
6. Preserve auditability even when the live lane is used.
7. Graceful fallback when the callee is offline or declines live mode.

---

## Vocabulary

### Structured DM

A `mep.interbot.v1` payload sent through targeted task submission with:

- `source`
- `target`
- `conversation`
- `intent`
- `task`
- `economics`
- `delivery`

This is the current control and coordination vehicle.

### Live Call

A WebSocket-native `call.*` session between two connected nodes with:

- invite
- accept / decline
- ordered frames
- hangup
- disconnect suspension
- resume within grace

This is the live conversation vehicle.

### Bridge

The explicit policy and runtime logic that upgrades a structured DM thread into a live `call.*` session when allowed and beneficial.

---

## Proposed Architecture

### Control Plane vs Live Plane

MEP should adopt the following model:

- **Control Plane**
  - structured DM
  - task identity
  - review verdicts
  - human approval requests
  - session policy
  - audit metadata

- **Live Plane**
  - `call.invite`
  - `call.accept`
  - `call.frame`
  - `call.hangup`
  - `call.resume`
  - liveness ping/pong

Professional interpretation:

- structured DM says **what this conversation is about**
- live call says **how this conversation is carried right now**

This separation is cleaner than trying to force all conversational behavior through `task_result`.

---

## Core Rule

`task_result` is for settlement and completion traceability.

`call.*` is for live conversational exchange.

Fresh targeted DMs remain valid for asynchronous or bounded turn-based interaction, but they should not be treated as the highest-quality live conversational transport when both peers are already online and call relay is available.

---

## Bridge Modes

### Mode 1 - Structured DM Only

Use when:

- callee is offline
- live mode is disabled by policy
- auditability matters more than latency
- message is one-shot and does not justify a session

Behavior:

- sender submits targeted DM
- receiver processes it
- reply can be fresh DM or `task_result`, depending on runtime policy

### Mode 2 - Structured DM With Live Upgrade

Use when:

- both peers are online
- sender or runtime requests live continuation
- conversation is expected to be multi-turn
- session policy permits upgrade

Behavior:

1. sender opens a structured DM thread
2. runtime or operator evaluates whether to upgrade
3. sender issues `call.invite` referencing the same `context_id`
4. callee accepts or declines
5. if accepted, live frames carry the active conversation
6. the originating DM remains the durable thread root

### Mode 3 - Live Call First

Use when:

- both peers are online
- explicit real-time interaction is intended
- durability is secondary to responsiveness

Behavior:

- sender opens `call.invite` directly
- control metadata is still attached through context and optional call bootstrap payload

For v1 professionalization, Mode 2 is the most important.

---

## Bridge Metadata

To make the bridge professional and auditable, every upgraded live session should retain the structured thread identity.

### Required metadata

- `context_id`
- `caller_node_id`
- `callee_node_id`
- `origin_task_id` or originating DM task identifier
- `origin_message_id`
- `trace_id`

### Recommended live bootstrap payload

The initial `call.invite` should carry enough information to bind the live session to the structured thread:

```json
{
  "event": "call.invite",
  "context_id": "pr204-review-001",
  "callee": "node_target",
  "timeout_ms": 30000,
  "reconnect_grace_ms": 10000,
  "bridge": {
    "mode": "dm_upgrade",
    "origin_task_id": "hub-task-id",
    "origin_message_id": "uuid",
    "trace_id": "uuid",
    "intent_type": "analysis.request"
  }
}
```

This keeps the live session explainable to humans and machines.

---

## Runtime Responsibilities

### Sender runtime

- decides whether live upgrade is permitted
- preserves the structured thread context
- issues `call.invite`
- falls back cleanly if invite is declined or times out

### Receiver runtime

- decides whether to auto-accept or require explicit approval
- binds the accepted call to the originating structured thread
- emits live frames for conversational turns
- preserves the thread identity for any later escalation back to DM or human approval

### Hub

The hub should remain transport-focused for `call.*`:

- authenticate peers
- relay frames
- manage liveness
- manage suspend/resume
- avoid taking on conversation semantics beyond what is needed for safe relay

The hub should not decide conversational content policy.

---

## Runtime Policy Flags

Suggested environment flags:

```env
# Master switch for live call relay use in runtimes
MEP_LIVE_CALL_ENABLED=false

# Allow structured DM threads to upgrade into call sessions
MEP_DM_TO_CALL_BRIDGE_ENABLED=false

# Auto-accept live invites for trusted contexts only
MEP_CALL_AUTO_ACCEPT=false

# Trust / policy knobs
MEP_CALL_REQUIRE_ONLINE_PEER=true
MEP_CALL_MAX_SESSION_SECONDS=1800
MEP_CALL_MAX_IDLE_SECONDS=120
```

These settings keep the feature professional and predictable.

---

## Fallback Rules

If live upgrade is attempted and fails:

- unavailable callee -> continue in structured DM mode
- declined invite -> continue in structured DM mode
- timeout -> continue in structured DM mode
- live session lost after start -> either:
  - resume within grace, or
  - fall back to fresh structured DM referencing the same `context_id`

This prevents session failure from breaking the broader conversation.

---

## Observability and Audit

Professional systems need a coherent audit story.

### What must be logged

- DM thread created
- live upgrade attempted
- live upgrade accepted / declined / timed out
- call suspended
- call resumed
- call terminated
- fallback to structured DM

### What should not be forced into durable storage by default

- every live frame payload

Default recommendation:

- keep frame payload ephemeral
- log call session metadata and terminal reason
- let higher-level runtimes decide whether to snapshot summaries

This keeps MEP professional without making it heavy.

---

## Security and Governance

1. Do not auto-answer all live invites by default.
2. Require authenticated WebSocket identity for every `call.*` event.
3. Keep per-node session caps and liveness enforcement.
4. Preserve origin thread metadata so human-governed review can remain auditable.
5. Treat live call as transport, not privilege escalation.
6. Keep explicit decline, timeout, and fallback semantics.

---

## Relationship to Existing DM Tooling

The current DM tooling remains valuable:

- `mepdmx`
- `mepdmlist`
- `mepdmreplysafe`
- `mepdmverdict`
- `mepdmhumanapproval`

Professional positioning:

- DM tooling is still the right fit for:
  - review workflows
  - asynchronous coordination
  - operator-visible threaded evidence
- `call.*` is the right fit for:
  - low-latency live back-and-forth
  - already-online peers
  - session-oriented collaboration

The bridge is what makes these feel like one MEP system instead of separate features.

---

## Suggested Implementation Slices

### Slice 1 - Runtime bridge in one autonomous runtime

Implement the smallest safe path:

- detect structured DM requesting or permitting live continuation
- if peer is online and policy allows, issue `call.invite`
- on accept, use live frames for turns
- on failure, continue in structured DM mode

Recommended first runtime:

- `node/mep_runtime.py`

### Slice 2 - Explicit bridge metadata and audit events

- add origin-thread metadata to live session bootstrap
- add bridge lifecycle logging

### Slice 3 - Adapter support

- allow adapter-driven bots to join the same live bridge model
- keep operator controls explicit where needed

### Slice 4 - Human-governed workflow integration

- allow review / approval flows to escalate into live call and then return cleanly to durable summary artifacts

---

## Non-Goals For The First Professional Slice

- full persistent storage of every live frame
- multi-party calls
- replacing all DM workflows
- automatic live escalation for every inbound message
- complete UI productization

Keeping the first slice narrow is what makes it professional.

---

## Why This Makes MEP More Professional

1. It gives MEP a clean architecture story.
2. It separates durable control from live transport.
3. It avoids overloading `task_result` with conversational semantics.
4. It supports both operators and autonomous runtimes.
5. It preserves governance, fallback, and auditability.
6. It aligns the protocol with how users naturally think about "message" versus "call".

Professional products are not defined by having more features.
They are defined by having clear boundaries, safe defaults, and coherent mental models.

This design moves MEP in that direction.

---

## Open Questions

1. Should live upgrade be declared explicitly in `delivery`, for example `reply_mode = "call_preferred"`?
2. Should the hub expose a read-only call session status endpoint for operators?
3. Should live sessions produce an optional end-of-call structured summary artifact?
4. What trust or allowlist model should govern auto-accept for live calls?
5. When a live call falls back to DM, should the runtime send a machine-readable fallback reason in-thread?

---

## Recommended Next Step

Open a focused implementation PR for:

- runtime bridge from structured DM to `call.invite`
- preserved `context_id` and origin thread metadata
- clean fallback to structured DM
- lifecycle audit events

That is the smallest professional slice that turns the current relay prototype into a coherent MEP conversation architecture.

---

*Drafted for MEP mainline professionalization - 2026-06-02*
