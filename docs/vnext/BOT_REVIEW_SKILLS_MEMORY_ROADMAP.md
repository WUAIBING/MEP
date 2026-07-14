# Bot Review Skills And Memory Roadmap

## Status

Draft roadmap for the next reviewer-evolution phase after the current prompt-governed review loop hardening.

---

## Summary

This roadmap defines how the MEP reviewer evolves from:

- prompt-governed GitHub review generation

into:

- structured review skills
- tool-augmented evidence collection
- node-local memory-aware review
- optional local-model fine-tuning and governed shared learning

The core sequence is:

1. make the current loop grounded and reliable
2. make the review procedure explicit and modular
3. add bounded review-specific work abilities
4. add node-local memory and distilled learning
5. only then promote validated learning into local-model optimization and cross-bot sharing

This document is intentionally roadmap-oriented. It defines sequencing, scope boundaries, and exit criteria. It does not propose a single combined implementation PR.

---

## Problem Statement

The current GitHub-to-MEP review loop has improved substantially, but its strongest behavior still relies on prompt discipline plus bridge-side gating. That is enough for the current stage, but it leaves four structural gaps:

1. Review procedure remains partly implicit inside prompts rather than explicit runtime skills.
2. The reviewer mostly reasons from supplied review context rather than collecting stronger evidence directly.
3. The current live Python runtime lacks a real node-local memory pipeline aligned with the earlier whiteboard design.
4. Shared learning and fine-tuning do not yet have a governed artifact pipeline.

If left unresolved, the loop can improve review style and suppression quality, but it will remain:

- too stateless
- too prompt-shaped
- too dependent on pre-packed inputs
- too fragile for cumulative learning

---

## Current State

### What Exists Today

- The GitHub bridge creates review tasks and writes review results back to GitHub.
- The runtime supports prompt-governed PR review and repo audit generation.
- The bridge already has meaningful gates for grounding, approval safety, and review quality.
- The runtime already uses a two-pass review pattern in practice:
  - candidate generation
  - verification / rendering

### What Does Not Exist Yet

- Explicit review skills as first-class runtime units
- Tool-augmented review evidence collection in the GitHub review loop
- A complete node-local memory pipeline in the current live Python runtime
- Typed distilled learning artifacts suitable for retrieval, sharing, benchmark promotion, or training
- A governed path from local learning to shared learning

---

## Design Principles

1. **Grounding before autonomy**
   - A stronger reviewer must first be more correct, not merely more active.

2. **Structure before power**
   - Explicit review skills should come before broad tool use.

3. **Local memory before shared learning**
   - A bot should learn safely on its own node before its lessons propagate.

4. **Benchmark before fine-tune**
   - No raw whiteboard or raw local memory should flow directly into model training.

5. **Owner-approved sharing**
   - Shared learning must be explicitly governed, sanitized, and versioned.

6. **One shared capability substrate**
   - GitHub bridge review and DM talk+work should converge on the same runtime/skills foundation, even if they retain different ingress and output paths.

---

## Roadmap

## V1: Prompt-Governed Review

### Goal

Make the current GitHub review loop grounded, publishable, and professional enough for real developer use.

### Scope

- Prompt tightening for review grounding
- Two-pass review behavior
- Approval safety rules
- Bridge-side suppression and scoring
- Professional no-finding review tone
- Reduction of obvious false-positive classes

### Why This Stage Exists

This is the lowest-risk, highest-ROI stage. It hardens output quality without requiring a new agent architecture.

### What Success Looks Like

- Grounded review findings publish reliably
- Weak speculative approvals are suppressed
- No-finding approvals read like compact professional reviewer notes
- The live loop maintains a stable high score without runtime regressions

### Non-Goals

- No broad tool execution
- No node-local learning pipeline
- No fine-tune workflow

---

## V2: Structured Review Skills

### Goal

Refactor implicit review behavior into explicit review skills with clear inputs, outputs, and validation boundaries.

### Scope

Representative skills:

- `review.discover_candidates`
- `review.verify_candidates`
- `review.render_publishable_output`

Each skill should have:

- typed input schema
- deterministic output contract
- explicit failure handling
- isolated test coverage

### Why This Stage Exists

The current prompt-governed loop already contains a review procedure. V2 makes that procedure explicit, modular, and easier to benchmark.

### What Success Looks Like

- Review generation is decomposed into named steps
- Each step can be tested independently
- Runtime behavior depends less on one oversized prompt block
- Bridge and runtime can reason about stage outputs explicitly

### Non-Goals

- No broad repo execution yet
- No memory-aware retrieval yet

---

## V2.5: Local Memory Layer Foundation

### Goal

Build the missing node-local learning substrate in the current live Python runtime before memory-aware review begins.

### Scope

- Structured local event capture in the live runtime
- Whiteboard-style append-only raw event log
- Typed distilled local learning artifacts
- Local retrieval / recall hooks for future runtime use
- Privacy and owner-approval metadata for later sharing

### Why This Stage Exists

V4 cannot exist without this foundation. The current whiteboard design established the concept, but the live Python runtime still lacks a full local memory pipeline.

### Minimum Components

#### Tier 1: Raw Local Event Log

- append-only
- node-local
- high-fidelity
- not training-ready

#### Tier 2: Distilled Local Artifacts

- lower-volume
- higher-signal
- typed
- reusable

Proposed artifact classes:

- `learning.insight`
- `learning.failure_case`
- `learning.corrected_review`
- `learning.benchmark_candidate`
- `learning.prompt_patch`
- `learning.training_candidate`

### What Success Looks Like

- The Python runtime writes structured local memory
- Distillation produces typed local artifacts
- A review task can retrieve local lessons relevant to the task
- Nothing leaves the node by default

### Non-Goals

- No automatic cross-node sync yet
- No direct fine-tune yet
- No mandatory shared-repo dependency

---

## V3: Tool-Augmented Review Skills

### Goal

Give review skills bounded work/evidence-collection ability so the reviewer can validate more claims against the repo itself.

### Scope

Examples of bounded review tools:

- repo search
- changed-file reads
- symbol lookup
- related-test inspection
- safe grep across repo
- targeted config / CI inspection
- optional narrow safe commands
- optional MCP-backed structured fetches

### Why This Stage Exists

Many weak reviews happen because the bot sees too little evidence. V3 improves evidence quality without turning the reviewer into an unrestricted coding agent.

### Boundary

This stage is **review-specific tool augmentation**, not full autonomous coding.

The V3 reviewer should be able to gather more evidence, but it should still operate under review safety/governance boundaries.

### What Success Looks Like

- The reviewer can confirm or reject hypotheses against real repo context
- False positives decrease on harder PRs
- Review outputs cite stronger evidence
- The same shared runtime can power both GitHub review tasks and DM talk+work evidence gathering

### Non-Goals

- No open-ended repo modification as part of normal review
- No automatic fix generation requirement

---

## V4: Memory-Aware Review Skills

### Goal

Make review skills use node-local learned experience during review.

### Scope

- retrieve relevant local distilled artifacts before or during review
- apply prior false-positive lessons
- apply repo-specific lessons
- apply corrected-review examples
- use prior benchmark-backed heuristics

### Why This Stage Exists

Without memory, the reviewer keeps starting cold. V4 turns the reviewer into a cumulative system that improves from repeated work.

### What Success Looks Like

- Repeated false positives drop over time
- Repo-specific review quality becomes more stable
- The reviewer remembers past corrections and avoids repeating them
- No-finding approvals become more consistent and less generic

### Non-Goals

- No automatic global sharing of local memory
- No direct model fine-tune from raw memory

---

## After V4: Optional Local Fine-Tune And Shared Learning

### Goal

Promote only validated learning into stronger local models and governed mesh-wide learning.

### Scope

- training-candidate generation from validated distilled artifacts
- benchmark gating before model promotion
- optional local-model fine-tune
- owner-approved artifact sharing
- mesh-wide adoption only after validation

### Why This Is Later

Fine-tuning before the earlier stages would train on noise, private data, and unstable review procedures. It belongs after the local memory and review-skill layers are already well-defined.

### What Success Looks Like

- only validated artifacts enter training sets
- local-model improvement has rollback and versioning
- shared learning is governed, sanitized, and benchmarked

---

## Whiteboard Repo Role

### Position

The shared whiteboard repo remains useful, but it should not be treated as the mandatory runtime memory substrate.

### Recommended Role

The whiteboard repo should act as:

- the shared artifact exchange layer
- the human-reviewable learning layer
- the owner-approved publication layer
- the mesh-level curated library

### What Should Stay Local

- raw whiteboard logs
- sensitive operational memory
- secrets and private task details
- low-confidence local notes

### What Can Be Shared

Only approved, sanitized, typed artifacts such as:

- benchmark candidates
- corrected review examples
- reusable lessons
- prompt/rule proposals
- training candidates after validation

### Bottom Line

The local memory layer is the primary learning substrate.

The whiteboard repo is the governed sharing layer above it.

---

## Shared Runtime Convergence

This roadmap intentionally moves GitHub bridge review and DM talk+work toward one shared capability substrate.

That does **not** mean the ingress paths become identical.

Instead:

- GitHub bridge remains a review/task ingress
- DM talk+work remains a conversational/task ingress
- both increasingly share:
  - runtime skills
  - tool abilities
  - local memory
  - model adapters

This convergence reduces duplicated logic and allows review quality improvements to compound across multiple entry paths.

---

## Suggested Implementation Order

### Track A: Review Loop Maturity

1. finish V1 hardening
2. extract V2 structured review skills
3. add V3 bounded tool augmentation
4. connect V4 local memory-aware review

### Track B: Learning Infrastructure

1. implement V2.5 local memory capture in Python runtime
2. add typed distilled artifacts
3. add local retrieval / recall hooks
4. add owner-approved share/export format
5. add benchmark promotion path
6. add optional local fine-tune path later

### Why Two Tracks

The review loop can keep improving immediately, but the learning substrate must be built in parallel so that V4 and later shared-learning work have a real foundation.

---

## First Recommended PRs

### PR 1: Docs Roadmap

This document.

### PR 2: Python Runtime Local Memory MVP

Minimum likely scope:

- structured local event logging in `node/mep_runtime.py`
- whiteboard file path and schema
- typed distilled artifact skeleton
- no cross-node sharing yet

### PR 3: Structured Review Skills Extraction

Minimum likely scope:

- explicit candidate discovery stage
- explicit verification stage
- explicit render/publishable stage

---

## Non-Goals For The Next Slice

- full autonomous code-fixing reviewer
- automatic global memory sharing
- direct fine-tuning from raw local logs
- replacing strong provider models with local small models immediately
- unbounded tool execution during review

---

## Exit Criteria For This Roadmap

The roadmap is considered successful when:

1. the review loop is explicit, bounded, and benchmarkable
2. the live Python runtime has a real local memory substrate
3. review skills can gather stronger evidence safely
4. local experience can improve future reviews
5. shared learning is governed rather than ad hoc

---

## Related Documents

- `docs/node-memory-layer/DESIGN.md`
- `docs/mesh-transparency/DESIGN.md`
- `docs/idle-autopilot/DESIGN_MAP.md`

