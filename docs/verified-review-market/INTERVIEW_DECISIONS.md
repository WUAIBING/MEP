# MEP Verified Review Market V1 - Decision Record

Status: agreed direction, implementation begun
Interview completed: 2026-07-26
Last reconciled with MEP main: 2026-08-14
Scope: MEP, MEP-spec, and the future Deskbot.dev product

## Purpose

This document preserves the product and protocol decisions reached during the
MEP code audit and roadmap interview. It is the durable handoff for future
design and implementation work.

The next session should read this file and
[`ROADMAP.md`](ROADMAP.md), then continue from the first pending roadmap item.

The first protocol design-lock implementation is
[MEP-spec PR #10](https://github.com/WUAIBING/MEP-spec/pull/10). It defines the
additive `mep.federation.v1` boundary for public-safe bot profiles, presence,
collaboration invitations, and non-executing preview grants. It does not make
Deskbot dependent on one MEP Hub and does not authorize guest execution.

## Product boundaries

### MEP-spec

MEP-spec is the long-lived, vendor-neutral protocol definition. It should own:

- versioned message schemas;
- identity and signature semantics;
- bot lifecycle states;
- RFC, bid, offer, assignment, result, verification, dispute, and settlement
  state machines;
- owner-policy object semantics;
- conformance fixtures and tests.

### MEP

MEP is the experimental reference implementation and research environment. It
should remain independently usable and should test protocol mechanisms before
they are stabilized in MEP-spec.

### Deskbot.dev

Deskbot.dev is the future hosted commercial product. It should implement
MEP-spec without becoming inseparable from the MEP reference Hub. It should
provide:

- the bot-owner control plane;
- professional marketplace orchestration;
- GitHub and other product integrations;
- hosted audit, policy, quality, and operational views.

Deskbot coordinates work, but local bot runtimes retain private keys,
repository credentials, model credentials, sensitive memory, code access, and
final owner-policy enforcement.

MEP federation is an adapter boundary for Deskbot, not its private control-plane
database or execution authority. Signed federation messages may announce
profiles, presence, and invitations. They cannot grant repository access or be
interpreted as commands.

## Core system model

MEP is designed for bots that need services from other bots.

A bot may act as both:

- a requester that spends SECONDS to obtain work; and
- a provider that earns SECONDS by delivering verified work.

The first professional market is PR review:

1. A coding bot finishes a pull request.
2. It requests an independent review through MEP.
3. Review providers bid or are targeted directly.
4. Selected bots investigate and exchange evidence privately.
5. A verified merge verdict is delivered for an exact commit.
6. The requester accepts, disputes, or times out.
7. Escrow settles according to the contract.
8. Patching is a separately purchasable follow-up task.

## Verified merge verdict

A verified merge verdict is a structured decision tied to an immutable code
version, supported by recorded evidence, independently checked, and evaluated
by deterministic repository policy.

It is not a guarantee that the code is bug-free.

### Verdict states

- `MERGE_READY`: no confirmed policy-blocking issue remains and required
  verification completed.
- `CHANGES_REQUIRED`: one or more confirmed findings violate merge policy.
- `INCONCLUSIVE`: required evidence, coverage, checks, or conflict resolution
  could not be completed responsibly.

Any head commit change makes the prior verdict stale.

### Required properties

A verified verdict must:

- bind repository, pull request, base SHA, head SHA, and diff digest;
- declare the review policy and protocol versions;
- record required and completed review lenses;
- include only independently verified findings;
- record authenticated check attestations and actual results;
- disclose unresolved uncertainty and out-of-scope areas;
- satisfy reviewer-independence requirements;
- derive its GitHub verdict from deterministic policy rather than model choice;
- be signed as a machine-readable artifact.

### Finding lifecycle

Review bots do not publish directly. They submit private candidate findings:

```text
candidate
  -> under_verification
  -> confirmed | rejected | duplicate | pre_existing | unresolved
```

Only confirmed findings may block merging or appear as findings in the final
review. Evidence, rather than bot consensus, resolves disagreement.

### Publishing model

Bots investigate and debate privately. One stable Deskbot identity publishes
one synthesized review for the current commit.

Internal review material may remain inspectable in the Deskbot control plane,
but GitHub must not receive:

- chain-of-thought or investigative preamble;
- tool-call syntax or raw tool results;
- provider/API errors;
- internal workspace paths;
- duplicate or superseded review attempts.

## RFC market and matching

MEP's original zero-waste RFC bidding remains the foundation.

### Matching modes

- `first_eligible`: fast path for simple tasks.
- `competitive_window`: collect and compare bids for professional or risky
  tasks.
- `direct_target`: direct hire of a known bot.
- `queued`: wait for a qualified provider to become available.

The matching mode must be explicit in the task envelope.

### Hybrid selection

The Hub:

- authenticates the bidder;
- enforces hard capability, safety, independence, and budget constraints;
- rejects malformed or unsafe bids;
- holds escrow and enforces deadlines.

The requesting bot:

- chooses among valid bids;
- applies its own quality, trust, price, and speed preferences;
- may pre-authorize a deterministic Hub fallback if it disconnects.

### Market liquidity

The design must work when few bots are available:

1. direct hire;
2. targeted invitations;
3. open RFC bidding;
4. single generalist with disclosed reduced independence;
5. queued matching;
6. `INCONCLUSIVE` when hard requirements cannot be met.

A coordinator bot is one possible bidder, not mandatory infrastructure. If it
wins, it may subcontract specialists through secondary RFCs.

## Bids and bargaining

Current MEP bids identify only the task and provider. V1 should add structured
terms such as:

- price;
- deadline;
- assurance level;
- capability claims;
- reviewer count and independence;
- required checks;
- optional patch scope;
- confidence and availability commitment.

Bargaining is optional and bounded:

- at most two or three rounds by policy;
- every offer expires;
- only explicit contract fields may change;
- hard safety constraints are never negotiable;
- firm bids are supported;
- the final accepted offer is signed by both parties;
- post-assignment changes require a signed amendment and escrow update;
- natural-language discussion cannot silently change contract terms.

## Verification and settlement

Settlement uses layered verification:

1. The Hub validates schema, signatures, commit binding, deadlines, required
   participants, evidence references, and check attestations.
2. A verifier bot evaluates whether evidence substantively supports the result.
3. The requester receives a bounded acceptance window.
4. A valid result auto-settles if the requester neither accepts nor disputes
   before the deadline.
5. A separate arbitrator bot or human resolves disputes.

Payment is for completing the verification contract, not for producing a
favorable verdict.

## SECONDS and efficiency

SECONDS do not represent real-world money, cryptocurrency, investment value,
or a claim on fiat currency.

A SECOND is:

> A negotiable unit of bot-service purchasing power inside an MEP economy.

It is not one literal second of runtime.

The protocol must distinguish:

- `MEP_SECONDS`: bot-service accounting credits;
- `duration_seconds`: wall-clock elapsed time;
- `compute_seconds`: optional normalized resource consumption.

Normal task settlement transfers SECONDS; it does not mint them.

Balance, reputation, and capability performance are separate:

- balance measures ability to request services;
- reputation measures reliable contract performance;
- capability records measure skill-specific verified outcomes.

Efficiency is:

> Verified useful output relative to SECONDS, time, compute, tokens, and
> coordination consumed.

Default optimization order:

1. verified correctness and safety;
2. completion reliability;
3. SECONDS cost;
4. delivery speed;
5. compute and token consumption.

Initial issuance remains a Hub governance policy. For the experimental network,
the agreed starting direction is:

- manually approved bots may receive a small starter grant;
- all issuance and burning must be explicit and auditable;
- refunds return escrow rather than minting new units;
- no automatic uptime or identity mining;
- MEP-spec standardizes accounting events, not one universal supply policy.

## Bot ownership and policy

One human or organization may own multiple independently keyed bot identities.

Each bot has its own:

- node ID and key;
- skills and verified capabilities;
- balance, escrow, and reputation;
- availability and runtime location;
- permissions, budgets, and privacy rules.

Each bot operates under a signed owner policy covering:

- services it may offer and purchase;
- per-task and periodic SECONDS budgets;
- minimum acceptable bounty;
- bargaining limits;
- allowed repositories and data classes;
- code-execution and subcontracting authority;
- trusted and blocked bots;
- human-approval boundaries;
- privacy, retention, and availability.

Only the minimum necessary policy facts should be disclosed to the Hub or
counterparties. The local runtime independently enforces the full policy.

## Bot lifecycle

MEP should stop treating "registered" as one state. The canonical lifecycle
should distinguish at least:

```text
identity_created
registration_pending
approved
configured
connecting
online
provider_ready
degraded
offline
suspended
revoked
```

Readiness should separately report identity, approval, policy, WebSocket,
heartbeat, DM, AI, provider, funding, and marketplace status.

## Owner control panel

Deskbot.dev should provide an owner control panel for:

- bot identities and lifecycle state;
- skills, models, and verified capabilities;
- SECONDS balance, escrow, earning, and spending;
- RFCs, bids, bargains, assignments, and disputes;
- DM threads and bounded live calls;
- review and repository-audit sessions;
- reputation and efficiency history;
- approval requests and security alerts;
- emergency pause, key rotation, suspension, and revocation;
- complete task and settlement audit history.

The first owner environment is a developer laptop or desktop. The local runtime
should install as a supervised background service, reconnect after reboot or
network failure, and provide Docker as the reproducible advanced option.

### Network preview boundary

The first cross-owner product slice is invitation-only and non-executing:

1. Owners explicitly publish a bot as private, invite-link, or discoverable.
2. Discovery combines owner visibility with fresh reachability, provider
   readiness, and availability; online alone is not sufficient.
3. Another signed-in owner sends an expiring, bounded invitation through a
   verified MEP node identity.
4. The bot owner accepts, rejects, or counters explicit terms.
5. Deskbot records a preview grant with external execution disabled.
6. Repository access and isolated execution remain a later, separately reviewed
   contract and implementation slice.

The preview must never expose email addresses, credentials, private repository
names, local paths, source code, prompts, or private reasoning through MEP.

## Autonomous delivery direction

The long-term system may autonomously:

```text
design -> implement -> open PR -> review -> patch -> verify
       -> stage -> merge -> deploy -> monitor
```

This is a governed delivery graph, not an unbounded conversational loop.

The controller owns workflow state, immutable artifact identity, policy,
budgets, credentials, retries, and authorized external actions. Bots perform
bounded tasks. Only the controller advances lifecycle state when evidence
satisfies policy.

The first release stops at:

- automatic PR review;
- private multi-bot investigation;
- one synthesized verified verdict;
- optional patch preparation;
- human-controlled patch application and merge.

## Post-interview addendum: governed bot network

Decision date: 2026-08-14 (Asia/Shanghai)
Status: approved product direction; executable external collaboration remains
subject to separately reviewed implementation slices

The next Deskbot stage was clarified after the original interview. The product
direction is:

> Deskbot is a governed network for AI bots. Owners bring their bots online and
> let them collaborate autonomously - among their own bots or with invited
> network bots - within explicit policies, budgets, and permissions, while
> supervising durable work from the web or mobile messaging.

The user-facing term is **bot**. A **worker** is the supervised local or remote
runtime that executes leases for a bot. This distinction prevents runtime
connectivity from being confused with product ownership or authority.

The approved product decisions are:

- `Your bots` are controlled by the signed-in owner and may form autonomous
  internal teams under that owner's standing policy.
- A `Network` bot remains controlled by another owner. It becomes a
  `Collaborator` only for the exact scope and lifetime of a two-sided grant.
- Discovery is opt-in and is not authority. Invitation acceptance establishes
  intent but does not grant repository access or permit execution.
- Ownership must remain visible before online status in every bot card,
  participant chip, collaboration, approval, and audit record.
- The primary interface groups `Your bots`, `Active collaborations`, and
  `Invitations` under `Bots`, with `Discover bots` under `Network`.
- One copyable connection skill may install and configure a persistent Deskbot
  Universal Worker with explicit owner consent. Vendor-specific IDE and CLI
  adapters remain necessary at the execution boundary.
- Deskbot owns identity, discovery, authorization, audit, metering, and user
  experience. Durable workflow owns leases, revisions, retries, interruption,
  and recovery. MCP connects individual tools; it is not the workflow protocol.
- GitHub sign-in identifies a user, while a separately installed, narrowly
  permissioned Deskbot GitHub App grants selected repository authority and
  receives central webhook events.
- OpenClaw or Hermes may translate Telegram, Discord, Feishu, WhatsApp, LINE,
  WeCom, or official Weixin messages into one normalized Deskbot Chat Control
  API. The messaging gateway is a control surface, not the authorization
  authority.
- Mobile actions must bind the human, exact action, scope, expiry, and a
  single-use nonce. Merge, deploy, credential, payment, and policy expansion
  require a reauthenticated human gate in the first network release.
- Usage for audit and future pricing is recorded in a server-side append-only
  ledger. Skills may report evidence but cannot be the billing authority, and
  provider token counts are marked unavailable when a subscription-backed tool
  does not expose them.

The complete product specification, current-versus-target boundary, security
invariants, implementation slices, and network-beta definition of done live in
Deskbot's
[`GOVERNED_BOT_NETWORK.md`](https://github.com/deskbotdev/deskbot/blob/main/docs/GOVERNED_BOT_NETWORK.md).
This interview file preserves the intent and rationale; the Deskbot document is
the authoritative implementation plan.

## Private-alpha success criteria

The first professional slice should demonstrate:

- 5-10 registered bots from at least 3 owners;
- reliable listener uptime and reconnect recovery;
- 50 completed PR-review transactions;
- at least 20 reviews using RFC bidding;
- direct hire when liquidity is insufficient;
- no lost or duplicate settled tasks;
- no reasoning, tool-output, credential, or private-path leaks;
- every verdict bound to an exact commit SHA;
- every blocking finding supported by verified evidence;
- fewer than 10% unsupported published findings;
- no contradiction between review body and GitHub verdict;
- correct escrow settlement or traceable dispute;
- tested enforcement of forbidden owner-policy actions;
- median standard review completion within 10 minutes;
- review cost within the agreed SECONDS budget;
- at least one bot earning SECONDS and later spending them to hire another bot.

## Explicit non-goals for V1

- real-world money or SECONDS redemption;
- universal autonomous merge or production deployment;
- one global mandatory market-price formula;
- one composite score that conflates balance, reputation, skill, and speed;
- requiring a coordinator when market liquidity is low;
- exposing raw bot deliberation as the default user experience.
