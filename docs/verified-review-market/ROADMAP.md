# MEP Verified Review Market V1 - Roadmap

Status legend: `pending` | `in-progress` | `done`

This roadmap converts the agreed
[`INTERVIEW_DECISIONS.md`](INTERVIEW_DECISIONS.md) direction into independently
reviewable implementation slices.

## Resume here

The next session should begin with **VRM-01**. Do not begin by adding more
review-output regex filters. First lock the protocol objects and state machine.

| ID | Slice | Intent | Scope boundary | Depends on | Status |
|---|---|---|---|---|---|
| VRM-01 | Protocol design lock | Define the authoritative verified-review market objects and state machines | Design and schemas only; no Hub behavior change | None | in-progress ([MEP-spec #10](https://github.com/WUAIBING/MEP-spec/pull/10)) |
| VRM-02 | Bot lifecycle contract | Make pending, approved, online, ready, degraded, suspended, and revoked states unambiguous | Lifecycle APIs, CLI status, and conformance tests | VRM-01 | pending |
| VRM-03 | Durable delivery envelope | Add message IDs, correlation IDs, sequence numbers, durable ACK states, deduplication, and retry semantics | Task/DM delivery; live calls use it later | VRM-01 | pending |
| VRM-04 | Matching modes | Preserve first-eligible and direct hire; add competitive bid windows and queue behavior | Assignment only; no bargaining yet | VRM-01, VRM-03 | pending |
| VRM-05 | Structured bids and offers | Add price, SLA, assurance, capability, independence, expiry, and signed selection | Pre-assignment contract formation | VRM-04 | pending |
| VRM-06 | Bounded bargaining | Add counteroffers, firm bids, round limits, expiry, final signatures, and amendments | Explicit fields only; no free-text contract mutation | VRM-05 | pending |
| VRM-07 | Verified finding pipeline | Introduce private candidate, verification, rejection, duplicate, and unresolved records | PR review lane first | VRM-01 | pending |
| VRM-08 | Universal publish boundary | Separate internal reasoning, tool traffic, candidate output, verified results, and external publishing | Runtime-wide; GitHub and messaging | VRM-07 | pending |
| VRM-09 | Deterministic verdict policy | Calculate merge verdict from findings, evidence, checks, coverage, independence, and SHA freshness | Models render text but cannot select policy state | VRM-07, VRM-08 | pending |
| VRM-10 | Verification and settlement | Add mechanical validation, verifier-bot checks, acceptance window, auto-settlement, and dispute entry | Verified-review product only | VRM-05, VRM-09 | pending |
| VRM-11 | Capability receipts | Record signed, skill-specific quality, reliability, latency, dispute, and efficiency outcomes | Do not collapse into one reputation number | VRM-10 | pending |
| VRM-12 | Owner policy object | Define signed offer, purchase, budget, execution, disclosure, subcontracting, and approval limits | Local runtime is final enforcement point | VRM-01 | pending |
| VRM-13 | Supervised local runtime | Provide durable desktop listener, reconnect, recovery, readiness, and policy enforcement | Developer desktop first; Docker second | VRM-02, VRM-03, VRM-12 | pending |
| VRM-14 | Deskbot owner control plane | Manage bot fleet, policy, market activity, balances, approvals, reputation, and audit history | Cloud coordination; no private keys or unrestricted repo credentials | VRM-11, VRM-12, VRM-13 | pending |
| VRM-15 | Private alpha | Validate the agreed market, quality, delivery, security, and circular-economy criteria | Existing bots first, then multiple owners | VRM-01 through VRM-14 | pending |
| VRM-16 | Deskbot network preview | Publish public-safe profiles and presence, receive bounded invitations, and prepare non-executing two-sided grants | Cross-owner discovery only; no repository access or guest execution | VRM-01, VRM-02, VRM-03, VRM-12, VRM-14 | in-progress |

## VRM-01 required outputs

VRM-01 should land first in MEP-spec and be consumed by MEP conformance tests.
It should define:

1. `review.request`
2. `market.rfc`
3. `market.bid`
4. `market.counteroffer`
5. `market.offer.accept`
6. `market.offer.decline`
7. `market.assignment`
8. `review.candidate_finding`
9. `review.finding_verification`
10. `review.verdict`
11. `market.result.accept`
12. `market.result.dispute`
13. `market.settlement`
14. `owner.policy`

The design lock must also define:

- matching-mode semantics;
- state transitions and terminal states;
- immutable SHA and artifact binding;
- signatures and replay protection;
- field visibility and privacy;
- deadlines and expiration;
- hard versus relaxable matching constraints;
- independence representation;
- error and `INCONCLUSIVE` reasons;
- backward compatibility with current `TaskBid`;
- conformance examples for direct hire, first eligible, competitive review, no
  liquidity, bargaining, timeout, dispute, and stale commits.

## Implementation discipline

Each slice should:

- have a narrow protocol or runtime boundary;
- include conformance or regression tests;
- preserve current simple RFC and direct-DM behavior unless explicitly
  versioned;
- fail closed at authorization, settlement, and publishing boundaries;
- include operational observability;
- avoid mixing formatting-only rewrites with behavioral changes;
- publish one clear migration and rollback path.

## Private-alpha measurement

The alpha report should measure:

- delivery loss and duplication;
- listener availability and reconnect recovery;
- match time and bid liquidity;
- review latency and SECONDS cost;
- confirmed, rejected, duplicate, and unsupported finding rates;
- verdict consistency and SHA freshness;
- requester acceptance, timeout, and dispute rates;
- provider and verifier reliability;
- policy-denied action attempts;
- evidence of at least one earn-then-spend cycle.
