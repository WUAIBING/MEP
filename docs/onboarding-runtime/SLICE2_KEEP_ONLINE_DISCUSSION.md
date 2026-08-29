# Slice 2 Keep-Online Reliability Discussion Draft

Use this document to turn the original Slice 2 idea from the PR 196 planning
thread into a focused bot-review discussion. The goal is not to redesign all
runtime architecture at once. The goal is to define the smallest mergeable next
step that makes real AI-backed nodes stay online more reliably.

## Background

The original slice plan defined:

```text
Slice 2: Keep-Online Reliability MVP
- one official real AI keep-online command path
- clearer status model
- reconnect/backoff polish
```

That plan came after Slice 1 addressed:

- repo-local identity continuity
- alias-safe registration
- honest `doctor` and `status` signals
- lazy optional imports
- safer sandbox bring-up

The main-chain destination is still the same:

- reliable fresh bot onboarding
- reliable keep-online behavior
- safe real AI-backed runtime bring-up
- then guarded real-world soak execution

## Why Slice 2 Exists

Slice 1 made fresh bring-up safer, but it did not fully answer the next
operator question:

> "Once my node is up, what is the one official way to keep it online
> reliably with a real adapter, and how do I know whether it is healthy?"

Today the repo has pieces of that answer, but not a single crisp operational
story yet.

## Current State

Already present in current `main`:

- `mep up` bootstraps `init -> doctor -> run`
- `mep run` supports `mock`, `ollama`, and `deepseek`
- the runtime reconnects WebSocket after errors
- the runtime exposes readiness badges including `AI_READY`
- call-bridge work added reconnect-related handling for live sessions

Relevant examples:

- `node/mep_runtime.py`
- `docs/onboarding-runtime/DESIGN.md`
- `docs/call-bridge/DESIGN.md`

## What Is Still Missing

The following parts still feel underdefined or incomplete:

1. **One official keep-online command path**
   - The repo has multiple runtime and provider entry points.
   - Bots should discuss which command path becomes the recommended default for
     real unattended listening.

2. **Clearer status model**
   - `status` and `doctor` improved in Slice 1, but the ongoing runtime health
     story is still thin.
   - Bots should discuss which states must be surfaced clearly:
     - registered
     - listener running
     - ws connected
     - heartbeat fresh
     - adapter configured
     - AI ready
     - degraded / reconnecting

3. **Reconnect/backoff policy**
   - Current reconnect is simple and fixed-delay.
   - Bots should discuss whether Slice 2 should add exponential backoff,
     jitter, max delay, and degraded-state reporting.

4. **Boundary between Slice 2 and later slices**
   - Slice 2 should improve keep-online reliability.
   - It should not accidentally absorb the whole autopilot, scheduler, or
     paid-mode guardrail problem.

## Proposed Slice 2 Scope

This draft recommends that Slice 2 cover only:

- one official real-AI keep-online command path
- clearer runtime health/status output for long-running mode
- reconnect/backoff polish for the runtime listener loop
- small extraction refactors only if they directly reduce Slice 2 risk

This draft recommends that Slice 2 not cover:

- paid unattended usage warnings
- runtime duration or spend caps
- broad hub module restructuring
- a full autopilot daemon rollout
- multi-process supervisor design
- durable work queues

## Recommended First Implementation Shape

### Step 1 - Declare the Official Keep-Online Path

Pick and document the one recommended operator command for real online bring-up.

Likely candidate:

```bash
python -m node.mep_runtime --hub-url <hub> --ws-url <ws> --adapter deepseek up
```

Bot review question:

- Is `mep up` the right official keep-online entry point, or should `run` be
  the official long-lived command and `up` remain bootstrap-only?

### Step 2 - Define Runtime Health States

Add a small, explicit runtime health model instead of relying only on loose
badge interpretation.

Suggested states:

- `offline`
- `bootstrapping`
- `connected`
- `degraded`
- `reconnecting`
- `stopped`

Bot review question:

- Which of these should be first-class operator-visible states in Slice 2, and
  which should remain internal detail?

### Step 3 - Reconnect And Backoff Policy

Replace the current fixed reconnect delay with a simple, explicit policy.

Suggested baseline:

- exponential backoff
- small random jitter
- max reconnect delay cap
- visible log/status when the node is degraded rather than silently retrying

Bot review question:

- What is the smallest reconnect policy that materially improves reliability
  without adding a new subsystem?

### Step 4 - Long-Running Health Output

Keep the runtime honest during long-running operation.

Suggested output/questions:

- should `status` expose last successful WS connect time?
- should `doctor` remain bootstrap-only, while `status` handles runtime health?
- should Slice 2 add a lightweight heartbeat freshness summary for operators?

## Allowed Refactor Boundary

Bots should assume this slice is **not** a license for a giant architecture
rewrite.

Allowed:

- extract a small reconnect helper
- extract runtime health/status helper logic
- extract small WebSocket loop helpers if they make the listener easier to test

Not allowed:

- broad `hub/main.py` modularization
- redesigning all provider runtime entry points
- mixing in Slice 3 warning/cost-cap policy
- pulling in the whole idle-autopilot design map

## Suggested Review Questions For Bots

Ask each bot to comment on:

1. What should be the one official real-AI keep-online command path?
2. Which health states must be surfaced to operators in Slice 2?
3. What reconnect/backoff policy is the smallest safe upgrade from the current
   fixed retry loop?
4. Which runtime failure mode is the most important to address first:
   socket drop, adapter misconfiguration, silent degraded mode, or alias /
   identity drift?
5. What refactor boundary keeps Slice 2 mergeable without turning it into a
   monolith rewrite?

## Proposed Deliverables

If bots agree on the direction, the first real Slice 2 implementation PR should
likely include:

- runtime keep-online path decision and docs
- reconnect/backoff helper or policy
- clearer runtime status/health reporting
- focused tests for reconnect and degraded-state behavior

## Non-Goals

This draft does not try to solve:

- reputation design
- finance migration
- paid-mode governance
- full daemonized autopilot
- large-scale hub restructuring

## Working Recommendation

The default recommendation for discussion is:

1. keep the PR 196 main chain intact
2. cut Slice 2 before reputation-track cleanup or large module refactors
3. keep Slice 2 operational and narrow
4. let Slice 3 handle warnings and bounded unattended usage later
