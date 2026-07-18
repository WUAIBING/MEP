# MEP Review And Audit Runtime V1.5 Roadmap

## Status

This document defines the `V1.5` stage between the current review runtime and a fuller `V2` investigative reviewer.

- `V1`: webhook-triggered PR review and repo-audit bots with workspace sync, prompt-driven review, bridge-side publication filters, and limited automated verification
- `V1.5`: controlled `talk + execute` tool wiring inside `node.mep_runtime`
- `V2`: consistently high-signal review and audit runtime that can investigate risky changes deeply, justify approvals with evidence, and stay quiet when evidence is weak

The goal of `V1.5` is not to maximize comment volume or speed. The goal is to make MEP PR review and repo-level audit materially more useful and trustworthy to developers.

## Problem

Current PR review and repo-audit quality are constrained by a structural gap:

- bots can reason over webhook payloads, diff excerpts, and selected workspace context
- bots can optionally run `ruff` and `pytest` in limited cases
- bots still cannot reliably perform the broader investigation flow needed for high-quality review and repo-level audit

This produces recurring failure modes:

- shallow "no issue found" reviews that only restate the diff
- approvals that are not backed by concrete checked behavior
- missed regressions when the changed hunk is low-risk in isolation but dangerous at call sites
- identifier-level grounding without state-transition reasoning

The root issue is not only prompt quality. The root issue is that the runtime still behaves more like a prompted reviewer than an investigative reviewer.

## Product Goal

Upgrade the review and audit runtime so a bot can:

1. inspect the exact PR head and authoritative workspace
2. expand from changed code to relevant call sites and dependent behavior
3. run safe targeted checks when risk justifies it
4. publish only findings or approvals that are grounded in evidence
5. reuse the same execution capability for both PR review and repo-level audit without duplicating infrastructure

`V1.5` should deliver a clear improvement in:

- finding correctness
- approval trustworthiness
- review usefulness to developers
- repo-audit usefulness to developers and operators
- bridge survival rate for valid reviews

## Non-Goals

`V1.5` does not try to do the following:

- unrestricted shell access
- arbitrary remote mutation during normal review
- automatic code patching as part of every PR review
- full repo-wide deep audit on every PR when the task only needs a focused review
- optimization for maximum speed before review quality is stable

## Design Principles

- Evidence before approval
- Controlled execution, not arbitrary execution
- Deep review only when risk justifies it
- Fail closed when evidence is weak
- Fewer, better review comments
- Safety boundary remains in the bridge even after runtime upgrades

## Current Baseline

Today the runtime already provides a useful base:

- webhook-triggered PR review task ingestion
- repo-audit task normalization and workspace sync support
- per-PR workspace sync via `git clone/fetch/checkout`
- local context pack assembly for touched files and tests
- optional `ruff` and `pytest` checks on the checked-out PR head
- two-pass candidate and verification prompting
- bridge-side suppression of weak or non-grounded writeback

This baseline should be kept and extended, not replaced.

## V1.5 Scope

`V1.5` adds a controlled execution layer to `node.mep_runtime` so reviewer and auditor bots can investigate code changes and repository structure instead of only narrating them.

The scope is:

- wire first-class review tools into the runtime
- make the same tool layer available to repo-level audit tasks
- define when each tool may be used
- inject tool output back into review reasoning in a structured way
- tighten approval rules so evidence becomes mandatory
- support parallel runtime lanes for focused PR review and broader repo audit
- preserve isolation and fail-closed behavior for unsafe or weak cases

## Dual-Lane Runtime Model

`V1.5` should treat PR review and repo-level audit as two first-class lanes running on the same execution foundation.

- `PR review lane`
  - diff-centered
  - decision-centered
  - optimized for approve, block, or grounded no-finding output

- `repo audit lane`
  - inventory-centered
  - architecture-centered
  - optimized for broader risk discovery, coverage reporting, and subsystem-level findings

The key design requirement is shared runtime infrastructure with lane-specific policy.

- the same tool wiring should serve both lanes
- the orchestration policy should differ by task type
- the publication contract should differ by task type
- both lanes should be able to run in parallel across separate tasks without blocking each other on design

This means `V1.5` work should be coded in parallel for:

- focused PR review quality
- repo-level audit depth and coverage

## Tool Surface

The initial `V1.5` tool surface should be intentionally narrow and high ROI for both PR review and repo audit.

### Required tools

- `workspace_read`
  - read exact files from the checked-out PR head
  - read targeted snippets around changed lines and matched symbols
  - support broader inventory-backed file reads for repo-audit tasks

- `workspace_search`
  - fast cross-file search for identifiers, call sites, config keys, and tests
  - default implementation can be `rg`; fallback can use Python search when unavailable
  - support wide cross-file pattern hunting for repo audits

- `workspace_git`
  - inspect tracked files, branches, commit metadata, and diff relationships inside the isolated workspace
  - no history rewriting
  - support branch/ref anchoring for repo-audit workspaces

- `targeted_verify`
  - run allowlisted checks such as `ruff`, `pytest`, and other narrow validation commands on the PR workspace
  - support subsystem-scoped verification during repo audits

- `github_context`
  - fetch richer PR metadata when webhook payload is insufficient
  - examples: changed file list, review threads, check state, PR body, linked discussions
  - when relevant, attach issue or audit-scoping context to repo-level audit tasks

### Optional V1.5 tools

- `second_pass_validator`
  - run an independent verification pass over one strong candidate finding

- `safe_shell`
  - allowlisted read/search/verify commands only
  - disabled by default for unsafe contexts

### Explicitly excluded from default PR review and audit

- unrestricted shell
- broad package installation during review
- arbitrary SSH into remote hosts
- direct file edits to the PR under review

## Tool Governance

Every tool in `V1.5` must have:

- an allowlist
- a timeout
- a structured output contract
- auditable execution logs
- a risk policy for trusted vs untrusted PRs

At minimum:

- untrusted PRs remain read-heavy and execution-light
- trusted PRs may run broader targeted verification
- secrets must never be exposed to PR-owned subprocesses
- tool failure must degrade to fail-closed review behavior, not fabricated confidence

## Review Workflow Changes

`V1.5` changes the runtime from "prompt first" to "investigate first".

The target review sequence is:

1. infer PR intent from title, body, changed files, and risk pack
2. classify the review as `discovery_review` or `recheck_review`
3. identify whether risky domains are touched
4. expand changed helpers or entry points to relevant call sites
5. run targeted checks if evidence is still incomplete
6. produce one of:
   - verified finding
   - grounded no-finding review
   - grounded approval

The runtime should prefer:

- one high-confidence finding over many weak observations
- one justified approval over generic praise
- no publication over bluffing

For repo-level audit, the equivalent preference is:

- one high-confidence subsystem finding over a large list of weak suspicions
- explicit coverage reporting over fake completeness
- no finding publication when the audited evidence does not clear the bar

## Repo-Audit Contract

`V1.5` should explicitly improve repo-level audit, not only PR review.

A valid repo-audit result should state:

- the audited scope, branch, or inventory boundary
- what files, subsystems, or code paths were deeply checked
- what was not deeply checked
- which findings survived verification
- why any near-miss candidate was withheld

Repo audit does not need the same publication contract as PR review.

- PR review is decision-centered
- repo audit is coverage-centered and architecture-centered

The runtime should share tools across both lanes while keeping these task contracts distinct.

## Deep-Review Triggers

`V1.5` should automatically enter deeper review mode when the PR touches:

- auth or identity logic
- approval or permission gates
- bridge routing or callback normalization
- CI gate handling
- task dispatch or writeback paths
- state machines or online/offline transitions
- money or billing paths
- secrets, config, or environment-driven trust

When any of these triggers fire, the runtime must require call-site expansion before approval.

## Approval Contract

The approval bar must be stricter in `V1.5`.

A valid approval should require:

- exact touched paths reviewed
- at least one concrete checked behavior or execution trace
- no surviving high-confidence candidate finding
- no contradiction from verification output

Examples of acceptable approval evidence:

- a helper change was expanded to all auth-sensitive call sites and no widened trust boundary was found
- changed tests and targeted checks support the reviewed path
- state transition remains consistent before and after the changed branch

Examples of invalid approval behavior:

- "looks scoped"
- "no obvious issue found"
- identifier narration without behavioral reasoning
- approval emitted before risky call sites are inspected

## No-Finding Contract

When no finding is published, the review must still be useful.

A valid no-finding review should state:

- what files or code paths were actually checked
- what risky behavior was explicitly considered
- why the reviewed evidence did not justify publication of a finding

The goal is not to force a bug on every PR. The goal is to make "no finding" trustworthy.

## Runtime Architecture Work

`V1.5` implementation work is grouped into five tracks.

### Track A: Tool wiring

- add first-class tool abstractions to `node.mep_runtime`
- return structured tool outputs instead of raw text blobs when possible
- keep tool execution inside isolated per-PR and per-audit workspaces

### Track B: Review policy

- codify deep-review triggers
- codify trusted/untrusted execution policy
- codify PR approval and no-finding publication contracts
- codify repo-audit publication and coverage contracts

### Track C: Review orchestration

- route from diff summary to helper/call-site expansion automatically
- choose targeted tools based on risk type
- support a second verification pass for strong candidates
- keep PR review and repo-audit orchestration lanes parallel in implementation so neither blocks the other

### Track D: Safety and auditability

- record tool invocations in runtime logs
- sanitize subprocess environments
- preserve bridge suppression as final safety boundary

### Track E: Evaluation

- benchmark current runtime vs `V1.5`
- score quality on real PRs and real repo-audit tasks, not only synthetic tests

## Rollout Plan

### Phase 0: Contracts and metrics

- define the initial tool API
- define PR review and repo-audit publication contracts
- define trusted/untrusted execution policy
- define the shared-runtime, dual-lane architecture boundary
- define review quality metrics

Exit criteria:

- tool contract approved
- review quality rubric approved
- safety policy approved

### Phase 1: Minimal tool surface

- implement `workspace_read`
- implement `workspace_search`
- harden `workspace_git`
- standardize `targeted_verify`
- validate the same tool surface against both PR review tasks and repo-audit tasks

Exit criteria:

- runtime can inspect exact PR head files and call sites on demand
- runtime can search for symbol usage without relying only on prompt context
- targeted verification output is structured and reusable

### Phase 2: GitHub and workflow enrichment

- add `github_context`
- feed richer PR metadata into the runtime
- improve risk-pack-driven tool selection
- improve inventory-driven tool selection for repo-audit tasks

Exit criteria:

- runtime no longer depends solely on webhook excerpts for PR understanding
- review intent and changed behavior are better anchored to the real PR

### Phase 3: Approval evidence gate

- require evidence for approval in risky review modes
- reject identifier-only approvals upstream
- require concrete execution trace or verified checked behavior before publish
- keep repo-audit publication separate so broad audit findings are not forced through PR-approval logic

Exit criteria:

- approval quality improves measurably
- weak approvals are rejected before bridge writeback

### Phase 4: Deep-review triggers

- activate automatic call-site expansion for high-risk changes
- add trust-boundary and state-transition review heuristics
- add stronger candidate verification for auth/bridge/runtime paths
- add broader subsystem and inventory coverage heuristics for repo audit

Exit criteria:

- runtime catches more real regressions like helper-to-call-site trust boundary widening
- shallow approvals on risky PRs drop materially

### Phase 5: Comparative evaluation and rollout

- compare current bots vs `V1.5` bots on real PRs
- compare current bots vs `V1.5` bots on real repo-audit tasks
- review false positives, missed findings, and approval trust
- roll out to primary reviewer bots after threshold is met

Exit criteria:

- `V1.5` beats baseline on finding correctness and approval trustworthiness
- developer usefulness improves in live PRs

## Milestones

- `M1`: runtime tool contract merged
- `M2`: `workspace_search` and structured verification live
- `M3`: `github_context` live
- `M4`: approval evidence gate live
- `M5`: deep-review triggers live
- `M6`: repo-audit contract and coverage reporting live
- `M7`: live side-by-side benchmark complete for both lanes
- `M8`: rollout to `Hub Sentinel` and `Elsaws`

## Success Metrics

Primary quality metrics:

- higher rate of grounded approvals that survive bridge publication
- higher recall on real risky regressions
- lower rate of generic no-finding reviews
- lower rate of false-positive blocker findings
- higher developer acceptance of bot review usefulness
- higher usefulness and coverage honesty for repo-level audit outputs

Operational metrics:

- median runtime latency remains acceptable
- tool timeouts remain bounded
- verification failures fail closed
- no secret leakage into PR-owned subprocesses

## Evaluation Plan

Use a real PR benchmark set with at least:

- clean low-risk PRs
- green focused bugfix PRs
- auth/identity/approval PRs
- bridge/runtime path PRs
- noisy but harmless refactors

Use a real repo-audit benchmark set with at least:

- focused subsystem audits
- architecture and trust-boundary audits
- config and secret-surface audits
- broad inventory-backed audits with partial-coverage reporting

For each run, score:

- finding correctness
- evidence quality
- approval trustworthiness
- false-positive rate
- publication survival rate
- usefulness to developers

Human-reviewed benchmark cases should remain the reference bar.

## Risks

- more tools may increase cost without increasing signal
- broader execution may create safety issues if policy is weak
- too much deep review may slow low-risk PRs
- richer tooling may still produce shallow reasoning if review policy is loose

## Mitigations

- wire tools in a narrow allowlisted order
- tie tool use to risk triggers
- keep approvals evidence-gated
- keep bridge suppression fail-closed
- benchmark on real PRs before wider rollout

## Definition of Done

`V1.5` is complete when:

- reviewer bots have controlled `talk + execute` capability inside the runtime
- auditor bots have the same controlled execution foundation with task-specific policy
- risky PRs automatically trigger deeper evidence gathering
- approvals require concrete checked behavior
- no-finding reviews are grounded and developer-useful
- repo-audit results are coverage-aware and developer-useful
- weak writeback is rare because runtime quality improved upstream

## Immediate Next Steps

1. Approve this roadmap direction.
2. Implement the runtime tool contract and the first allowlisted tool set.
3. Code PR review lane and repo-audit lane in parallel on top of the same runtime tool surface.
4. Add the approval evidence gate before expanding the tool surface further.
5. Benchmark `Hub Sentinel` and `Elsaws` against the current baseline on real PRs and real repo-audit tasks.
