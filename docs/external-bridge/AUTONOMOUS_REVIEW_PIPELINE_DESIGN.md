# GitHub-to-MEP Autonomous Review Pipeline - Design Document

## Status

Draft — intended for draft PR review before implementation.

---

## Summary

Define a production-grade autonomous code review pipeline over the existing GitHub -> MEP bridge.

The core idea is:

- GitHub is the developer-facing ingress and writeback surface
- MEP is the orchestration, specialization, and verification fabric
- the bridge is not just transport; it is the review control plane

This design is explicitly aimed at a real developer product, not a toy demo. The goal is not merely to post automated review text, but to produce reviews that are concrete, evidence-backed, and useful to working developers.

---

## Why This Should Start As A Draft PR

Yes, this is proper to open as a draft PR first.

Reasons:

1. The change is cross-cutting:
   - bridge payload design
   - runtime prompt contract
   - output gating
   - review writeback policy
   - metrics and evaluation
   - future multi-node role orchestration

2. The most important decisions are architectural, not implementation-local:
   - what evidence the bridge must gather
   - what a node is allowed to publish
   - when the system should refuse to post
   - when autonomous approval is allowed

3. A draft PR lets the team review product intent separately from code mechanics.

4. A docs-first draft avoids the common failure mode where a prototype evolves into production behavior without a clear contract.

Recommended PR shape:

- docs-only
- draft PR
- explicitly marks open questions
- no behavior promises until evaluation thresholds are agreed

---

## Problem Statement

The current GitHub -> MEP autonomous review loop can:

- receive a valid trigger
- fetch bounded PR context
- send a review task to a target node
- write review output back to GitHub

This proves the transport loop works, but transport correctness is not the same as product usefulness.

The current gap is:

- the system can produce reviews that sound plausible
- but many reviews are still too generic to be genuinely valuable to developers

Common failure modes:

- paraphrasing the diff instead of reviewing it
- generic praise without evidence
- weak "looks good" summaries
- failure to mention concrete files, tests, or changed behaviors
- publishing low-signal output that should have been retried, downgraded, or withheld

For a real product, the bridge must become an orchestration and quality-control layer, not just a forwarding pipe.

---

## Product Goal

The product should consistently produce autonomous review output that saves developers time.

Success condition:

- on a typical PR, the system usually produces at least one concrete, correct, useful observation tied to the actual diff

Non-goal:

- maximizing the number of posted bot reviews

Better to post fewer reviews with trustable content than many reviews with generic filler.

---

## Desired Product Outcome

Developers should experience the system as:

- a reliable first-pass reviewer
- a useful second opinion
- a concrete change summarizer
- a safe assistant that knows when not to overclaim

The system should eventually support:

- autonomous comment reviews
- conditional autonomous approval for low-risk PRs
- specialist reviewer nodes
- internal multi-pass verification before GitHub writeback

But the first production bar is usefulness, not full autonomy.

---

## Design Principles

1. Evidence first, style second.
2. Retrieval quality matters more than clever wording.
3. Multi-step review beats single-pass generation.
4. The system must know when not to post.
5. Approval requires a stricter standard than commenting.
6. MEP should be used for orchestration and specialization, not only transport.
7. Every published claim should be traceable to supplied evidence.
8. Generic output is a failure mode, not a successful review.

---

## Core Architectural Idea

Treat the bridge as a review control plane with five responsibilities:

1. ingest GitHub events
2. build a structured review package
3. orchestrate one or more MEP reviewer tasks
4. verify and score candidate output
5. write back to GitHub only when quality is sufficient

This yields a pipeline:

```text
GitHub webhook
  -> Bridge normalization
  -> Review package builder
  -> MEP review plan task
  -> One or more reviewer nodes
  -> Verifier / critic stage
  -> Writer / formatter stage
  -> GitHub writeback gate
```

---

## Review Package

The bridge should send a structured review package, not just a trigger sentence plus a small patch.

### Required Fields

- repository
- PR number
- title
- author
- current head SHA
- trigger source and intent
- changed files
- patch excerpts
- PR description
- PR stats

### Strongly Recommended Fields

- surrounding code context for changed hunks
- touched symbols/functions/classes
- touched tests
- impacted callers/callees for changed symbols
- previous review findings on the same PR revision
- language/runtime hints
- risk tags
  - auth
  - persistence
  - money flow
  - concurrency
  - API contract

### Review Package Rule

If the bridge cannot collect enough evidence to support a meaningful review, it should reduce scope or defer writeback rather than pretending to have performed a strong review.

---

## Review Task Types

The bridge should stop modeling all review work as one generic "review this PR" task.

Recommended MEP task families:

- `code.review.plan`
  - decide which review lenses to apply
- `code.review.findings`
  - generate candidate findings and observations
- `code.review.verify`
  - check whether each finding is supported by provided evidence
- `code.review.compose`
  - write the final GitHub-facing review body
- `code.review.approval`
  - approval-only decision with stricter gating

This enables a real pipeline instead of a single overburdened prompt.

---

## Node Roles

MEP is valuable here because it can support specialist reviewer nodes rather than one monolithic reviewer.

### Minimum Viable Roles

- **Planner**
  - reads the review package
  - decides which review passes are needed

- **Reviewer**
  - produces candidate findings and concrete observations

- **Verifier**
  - rejects unsupported claims
  - scores confidence and usefulness

- **Writer**
  - turns approved findings into concise GitHub review output

### Future Specialist Roles

- correctness reviewer
- test reviewer
- security reviewer
- API contract reviewer
- concurrency reviewer
- performance reviewer

The early production system can collapse some of these roles into one runtime, but the protocol should be designed so they can split later without redesign.

---

## Output Contract

The bridge should require structured review output from nodes.

Recommended schema:

```json
{
  "summary": "string",
  "observations": [
    {
      "kind": "non_blocking | blocking | note",
      "file": "string",
      "symbol": "string",
      "detail": "string",
      "evidence": ["string"]
    }
  ],
  "findings": [
    {
      "severity": "high | medium | low",
      "file": "string",
      "symbol": "string",
      "issue": "string",
      "why_it_matters": "string",
      "evidence": ["string"],
      "confidence": 0.0
    }
  ],
  "tests_reviewed": ["string"],
  "touched_paths": ["string"],
  "approval_recommendation": "approve | comment | request_changes | abstain"
}
```

Important rule:

- every finding must carry evidence
- every approval recommendation must explain what was checked

---

## Verification And Gating

This is the most important part of the design.

The bridge should not automatically trust a node's first answer.

### Gating Rules

Do not publish a review when:

- the output is generic
- no concrete file/test/path is mentioned
- the finding is unsupported by supplied evidence
- the node says "missing context" but still acts confident
- the output is mostly paraphrase without review value

### Retry / Downgrade Behavior

If the review is too weak:

1. retry once with richer context
2. if still weak, downgrade to an internal-only result
3. optionally publish a limited summary without strong verdict language

### Approval Gate

Approval should require all of:

- no supported blocking findings
- at least one concrete statement about what was checked
- at least one touched path or test path mentioned
- confidence above threshold
- no unresolved verifier objections

Autonomous approval should be harder than autonomous comment review.

---

## What A Public GitHub Review Must Include

Every published review should include most of the following:

- what changed
- what was checked
- one concrete observation
- blocking findings, or explicit no-blocking-issue statement
- tests reviewed

This is the minimum viable usefulness contract.

---

## Confidence Model

Each candidate review should receive at least two scores:

- **evidence confidence**
  - how well claims map to supplied files, hunks, tests, and symbols

- **usefulness score**
  - whether the review says something concretely valuable to a developer

Suggested writeback policy:

- publish strong comment if both scores pass threshold
- publish approval only if approval-specific threshold passes
- otherwise retry or hold

---

## Human Override And Safe Modes

The system should support modes:

- `observe`
  - generate internal review only
- `comment`
  - allow GitHub review comments, no approval
- `approve_low_risk`
  - approval only on low-risk PR classes
- `full_manual_gate`
  - human must explicitly request approval

For production rollout, default should be `comment`, not automatic approval.

---

## Metrics

Do not measure success as "did a review get posted".

Measure:

- percentage of reviews with at least one concrete observation
- percentage of reviews mentioning real touched files
- percentage of reviews mentioning touched tests when tests changed
- false positive rate
- approval reversal rate
- developer helpfulness feedback
- retry rate due to low-quality first pass
- publish suppression rate

These metrics should exist before claiming production readiness.

---

## Rollout Plan

### Phase 1 - Better Single-Node Reviews

- richer review package
- structured JSON response contract
- generic-output gate
- touched file / test mention requirement

### Phase 2 - Verification Layer

- internal verifier pass
- confidence scoring
- stronger approval gate

### Phase 3 - Multi-Node Orchestration

- planner node
- reviewer node
- verifier node
- writer node

### Phase 4 - Productization

- per-language policies
- repo-level risk policy
- tenant settings
- evaluation dashboards

---

## Open Questions

1. Should verifier output always be internal-only, or visible as a GitHub review note when it blocks publication?
2. How much surrounding context can the bridge safely retrieve without exploding token cost?
3. Should approval ever happen without at least one changed test, or is that too strict?
4. Should nodes compete independently and the bridge choose the best review, or should specialist roles collaborate in one plan?
5. Should the bridge keep an internal cache of past accepted findings to help calibrate future confidence?

---

## Recommendation

Proceed with this as a docs-only draft PR first.

That PR should:

- define the review package contract
- define output gating
- define the approval policy
- define metrics
- explicitly separate "posted review" from "useful review"

Only after agreement on those contracts should implementation continue.

---

## Final Position

If this is meant to become a real developer product, the GitHub -> MEP bridge must be designed as:

- ingress
- retrieval
- orchestration
- verification
- writeback control

not merely:

- trigger
- prompt
- post

That distinction is what separates a toy autonomous reviewer from a product developers might actually trust.
