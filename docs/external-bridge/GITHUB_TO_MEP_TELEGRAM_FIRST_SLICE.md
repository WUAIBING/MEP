# GitHub To MEP Bridge - First Slice

## Status

Draft.

This document defines the first practical external-platform bridge for MEP:

- inbound source: GitHub webhooks
- execution plane: MEP structured DM and targeted `new_task`
- live continuation: existing DM-to-call bridge when enabled
- visible operator surface: Telegram status messages

The goal is to solve the current Hub Sentinel failure mode:

- GitHub events already arrive
- Telegram notifications already show up
- but the event is not converted into an actionable MEP conversation

This slice fixes that gap without redesigning the whole system.

## Goal

Ship a reusable bridge that lets a bot owner deploy one component and get this behavior:

1. GitHub PR or Issue webhook reaches the bridge.
2. The bridge validates and normalizes the event.
3. The bridge submits a targeted `mep.interbot.v1` DM or `new_task` into MEP Hub.
4. The target bot, such as `Hub Sentinel`, receives an actionable event.
5. The target bot can stay in structured DM mode or upgrade into live `call.*`.
6. Telegram shows status updates for visibility and operator trust.

## Why This First Slice

This is the right first slice because:

- GitHub is already the real event source in the current workflow.
- Telegram is already the visible operator surface.
- MEP already has the core building blocks:
  - targeted `new_task`
  - structured `mep.interbot.v1` DM
  - DM queue / offline delivery
  - optional live `call.*` bridge
- the missing piece is the external platform adapter layer

This slice should not try to solve every external platform at once.

## Non-Goals

This first slice does not include:

- full multi-platform support in one PR
- a new UI product
- replacing all existing Telegram adapter behavior
- persistent storage of every live frame
- arbitrary bot discovery beyond explicit routing config
- automatic support for every GitHub event type

## Product Outcome

After this slice:

- GitHub mention-driven review requests can trigger autonomous MEP workflows
- Telegram remains the human-visible status surface
- the same bridge pattern can later be reused for Discord or other IM systems

## Core Principle

Telegram is not the execution trigger.

Telegram is the visibility and status surface.

MEP is the execution plane.

So the correct flow is:

`GitHub webhook -> bridge -> MEP task/DM -> bot runtime -> optional call.* -> GitHub action/result -> Telegram status`

Not:

`GitHub webhook -> Telegram only`

## Architecture

### Components

The first slice introduces one new layer:

- **External Bridge**
  - validates GitHub webhook signature
  - normalizes GitHub event into internal bridge event
  - resolves target node / bot
  - builds `mep.interbot.v1` payload
  - submits targeted task to MEP Hub
  - emits Telegram status updates

Existing components remain:

- **MEP Hub**
  - receives targeted task submission
  - routes `new_task` to online target
  - queues DM if target is offline
  - preserves auditability

- **Target Bot Runtime**
  - receives `new_task`
  - processes structured DM
  - may upgrade to `call.*` if policy allows
  - returns result through existing workflow

- **Telegram**
  - receives status updates:
    - event received
    - review started
    - review completed
    - failed / fallback / manual approval required

### High-Level Flow

1. GitHub sends webhook to the bridge endpoint.
2. Bridge validates signature and event type.
3. Bridge extracts repo, PR/Issue number, actor, mention target, URLs, and action details.
4. Bridge decides whether the event is actionable.
5. Bridge resolves target bot node.
6. Bridge builds a structured MEP message with stable `context_id`.
7. Bridge submits targeted task to MEP Hub.
8. MEP Hub routes it to the target bot as `new_task`.
9. Target bot processes the message.
10. If enabled, runtime upgrades to `call.invite` / `call.frame`.
11. Telegram receives visible status messages throughout.

## First-Slice Event Scope

### Supported GitHub Events

Start with:

- `pull_request`
- `issue_comment`
- `pull_request_review_comment`
- optionally `issues`

### First-Slice Actionable Cases

Only treat an event as actionable when at least one rule matches:

- PR body contains an actionable bot invocation
- Issue body contains an actionable bot invocation
- comment contains an actionable bot invocation
- repo-specific rule says all PR open/sync events should trigger the bot

### Example Actionable Triggers

- `@Hub-Sentinel review this PR`
- `@Hub-Sentinel please analyze this issue`
- repo-level rule: all PR open events go to `Hub Sentinel`

### Trigger Grammar

Simple mention matching is not strong enough for autonomous action.

Examples of weak signals:

- typoed alias
- ambiguous multi-bot mention
- sarcastic or negative mention
- informational mention with no requested action

Recommended first-slice rule:

- parse `@<bot-alias> <imperative verb> ...` as the minimum actionable unit
- non-imperative mentions may generate notification-only behavior, but should not auto-trigger execution

Examples:

- `@Hub-Sentinel review this PR`
- `@Hub-Sentinel analyze this issue`
- `@Hub-Sentinel check this PR`
- `@Hub-Sentinel comment on this PR`
- `@Hub-Sentinel approve`
- `@Hub-Sentinel triage this issue`

Recommended first-slice verb mapping:

- `review` -> `code.review.request`
- `analyze` -> `analysis.request`
- `check` -> `code.review.request`
- `comment` -> `code.review.comment`
- `approve` -> `code.review.request` with requested outcome hint
- `triage` -> `issue.triage.request`

For this first implementation, keep imperative verbs single-word. Phrases such as `request changes` remain a follow-up extension unless the parser is explicitly widened.

If a repo-level policy triggers all PR open/sync events automatically, that policy should be explicit in routing configuration rather than inferred from free text.

## Normalized Bridge Event

Every inbound GitHub webhook should be converted into an internal normalized shape before MEP submission.

Example:

```json
{
  "source_type": "github",
  "event_name": "pull_request",
  "delivery_id": "uuid-from-github",
  "bridge_id": "br-abc123",
  "repo_full_name": "owner/repo",
  "installation_id": null,
  "action": "opened",
  "actor": "octocat",
  "target_alias": "Hub Sentinel",
  "target_node_id": "node_123",
  "context_id": "github-owner-repo-pr-226",
  "thread_key": "owner/repo#226",
  "event_sequence": 17,
  "kind": "pull_request",
  "number": 226,
  "title": "Add live DM bridge",
  "html_url": "https://github.com/owner/repo/pull/226",
  "body_excerpt": "@Hub-Sentinel please review this PR",
  "actionable": true
}
```

This normalized event is the contract between GitHub parsing and MEP submission.

## Correlation Model

The bridge must carry three different identities through the workflow:

- `delivery_id`
  - GitHub delivery identity
  - used for webhook replay protection and dedup at ingress
- `context_id`
  - MEP conversation identity
  - used to keep the DM thread and optional live call on one durable thread
- `bridge_id`
  - bridge-owned workflow identity
  - used to correlate one bridge request from webhook ingress to runtime result
- `event_sequence`
  - bridge-visible event ordering value for a stable GitHub thread
  - used to distinguish fresh PR updates from older or superseded ones

Recommended rule:

- the bridge generates a fresh `bridge_id` for each accepted actionable bridge request
- the bridge persists it before submitting to MEP
- the runtime echoes it back in bridge status callbacks

Preferred status authentication model:

- pass `status_endpoint` plus a short-lived signed `status_token` in `bridge_metadata`
- avoid hardcoding bridge callback URLs inside the runtime
- avoid long-lived plaintext secrets per execution unless there is no better transport option
- recommended first-slice token format: HMAC-SHA256 signed token with claims `{bridge_id, target_node_id, exp}`
- recommended first-slice token lifetime: expected bridge execution time plus grace, approximately 30 minutes by default

This separation matters because:

- multiple GitHub deliveries may relate to the same PR thread over time
- one durable `context_id` may have several bridge executions
- the bridge still needs one stable key to link `review requested` to `review completed`
- a stable thread may still need an execution-local ordering hint

## MEP Payload Shape

The bridge should submit a targeted `mep.interbot.v1` message to the bot.

Recommended intent:

- `code.review.request` for PR review
- `analysis.request` for issues
- `coordination.request` for operational follow-up

Example payload:

```json
{
  "spec_version": "mep.interbot.v1",
  "message_id": "uuid",
  "trace_id": "uuid",
  "timestamp_ms": 1760000000000,
  "source": {
    "node_id": "bridge-github"
  },
  "target": {
    "node_id": "node_123"
  },
  "conversation": {
    "context_id": "github-owner-repo-pr-226",
    "turn_type": "review_request"
  },
  "intent": {
    "type": "code.review.request",
    "priority": "high"
  },
  "task": {
    "instructions": "Review PR #226 and decide whether to comment, request changes, or approve.",
    "inputs": {
      "github_event": {
        "repo_full_name": "owner/repo",
        "kind": "pull_request",
        "number": 226,
        "action": "opened",
        "title": "Add live DM bridge",
        "html_url": "https://github.com/owner/repo/pull/226",
        "actor": "octocat",
        "body_excerpt": "@Hub-Sentinel please review this PR"
      },
      "bridge_metadata": {
        "bridge_id": "br-abc123",
        "source_type": "github",
        "delivery_id": "uuid-from-github",
        "source_event": "pull_request",
        "source_action": "opened",
        "status_endpoint": "https://bridge.example.com/bridge/status",
        "status_token": "signed-short-lived-token"
      }
    },
    "expected_output": {
      "result_type": "text"
    }
  },
  "economics": {
    "bounty_ns": 0,
    "currency": "MEP_NS"
  },
  "delivery": {
    "reply_mode": "new_dm",
    "settlement_mode": "task_result"
  }
}
```

## Context Identity

Every GitHub object must map to a stable MEP thread root.

Recommended rules:

- PR: `github-<owner>-<repo>-pr-<number>`
- Issue: `github-<owner>-<repo>-issue-<number>`

Recommended companion fields:

- `event_sequence`
  - monotonic per `context_id` from bridge persistence
- or `review_epoch`
  - a bridge-defined integer that increases when a materially new review-worthy event arrives

This matters because:

- DM thread identity must remain stable
- later `call.*` upgrade needs the same `context_id`
- Telegram status updates should refer to the same context
- later comment or review events should continue the same thread

`context_id` is not a replacement for `bridge_id`.

Use:

- `context_id` for the durable conversation thread
- `bridge_id` for one bridge workflow execution
- `event_sequence` for ordering and supersession checks within the same thread

This allows the runtime or bridge to distinguish:

- same PR thread, newer commit push
- same PR thread, older stale queued review
- same PR thread, follow-up comment on the current review state

## Bridge Status Contract

To close the loop, the runtime must be able to report bridge-visible status back to the bridge.

Recommended endpoint:

`POST /bridge/status`

### Required Request Fields

```json
{
  "bridge_id": "br-abc123",
  "context_id": "github-WUAIBING-MEP-pr-226",
  "target_node_id": "node_123",
  "task_id": "task_456",
  "status": "completed",
  "action": "approved",
  "timestamp_ms": 1760000000000
}
```

### Recommended Additional Fields

```json
{
  "delivery_id": "uuid-from-github",
  "event_sequence": 17,
  "result_summary": "Approved after automated review.",
  "github_result": {
    "comment_posted": true,
    "review_submitted": true,
    "review_decision": "approve"
  },
  "error": null
}
```

### Status Semantics

Recommended `status` values:

- `accepted`
- `queued`
- `started`
- `completed`
- `manual_approval_required`
- `failed`

Recommended `action` values:

- `review_requested`
- `commented`
- `approved`
- `changes_requested`
- `analysis_completed`
- `no_action`

### Why This Endpoint Exists

Without `bridge_id`, bridge status calls are anonymous and the bridge cannot reliably determine which bridge execution they belong to.

With `bridge_id`, the bridge can:

- link `review completed` to `review requested`
- update Telegram status accurately
- persist a bridge execution timeline
- deduplicate repeated status calls
- diagnose partial or failed workflows

### Authentication And Idempotency

`POST /bridge/status` must be authenticated.

Recommended first-slice approach:

- the bridge includes `status_endpoint` and `status_token` in inbound `bridge_metadata`
- the runtime echoes the token in the callback
- the bridge verifies token validity, expiry, and target binding
- use an HMAC-SHA256 signing key controlled by the bridge for first-slice token verification

Recommended idempotency approach:

- key status writes by `bridge_id` plus status phase
- or by `bridge_id` plus a dedicated status event ID
- repeated callback delivery must be safe and non-destructive

## Routing Model

The bridge should support explicit routing config.

### First-Slice Routing Inputs

- default target node ID
- default target alias
- optional repo-specific target override
- optional event-type-specific override

### Example

```json
{
  "default_target_alias": "Hub Sentinel",
  "default_target_node_id": "node_123",
  "repo_rules": {
    "WUAIBING/MEP": {
      "target_alias": "Hub Sentinel",
      "target_node_id": "node_123",
      "pull_request": {
        "trigger_mode": "mention_or_open"
      },
      "issues": {
        "trigger_mode": "mention_only"
      }
    }
  }
}
```

### Trigger Policy Suggestions

Recommended first-slice policy knobs:

- `mention_only`
- `imperative_mention_only`
- `mention_or_open`
- `maintainer_only`

For production use, prefer `imperative_mention_only` or a repo-explicit automation policy over raw mention matching.

## Actor Authorization

The bridge should define who is allowed to trigger autonomous behavior.

Recommended options:

- allow all repo commenters
- allow maintainers only
- allowlist explicit GitHub usernames or teams

For the first slice, a repo-level `maintainer_only` option is strongly recommended for sensitive repositories.

## Telegram Role In First Slice

Telegram remains important, but not as the execution trigger.

### Telegram Should Show

- GitHub event received
- target bot selected
- task submitted to MEP
- target bot online / queued
- review started
- review completed
- fallback or failure reason

### Example Messages

- `GitHub event received: PR #226 in WUAIBING/MEP`
- `Hub Sentinel review task submitted: context=github-WUAIBING-MEP-pr-226`
- `Hub Sentinel review started for PR #226`
- `Hub Sentinel completed review for PR #226`
- `Hub Sentinel requires manual approval for PR #226`

This keeps Telegram useful while making MEP the real action engine.

### Message Volume Policy

Sending a new Telegram message for every phase is easy to implement but noisy.

Recommended first-slice default:

- create one status message per bridge execution
- edit that message as status progresses

Example lifecycle:

- `PR #226 review status: received`
- edit to `PR #226 review status: started`
- edit to `PR #226 review status: completed`

Recommended modes:

- `compact`
  - editable progress message
- `verbose`
  - separate messages for each transition, useful during rollout and debugging

## Deployment Model

### Recommended Form

Use a standalone bridge worker or isolated bridge module.

This is preferred because it is:

- easier to deploy
- easier to test
- easier for other bot owners to reuse
- easier to extend to Discord later

### Runtime Requirements

The bot or bot owner already knows:

- target node ID
- bot token
- GitHub PAT or App credentials
- GitHub webhook secret
- MEP Hub URL

That is enough for the first slice.

## Bot Owner Deployment Guide

### Required Environment Variables

Example:

```env
MEP_HUB_URL=https://mep-hub.example.com
MEP_WS_URL=wss://mep-hub.example.com
MEP_BRIDGE_SOURCE_NODE_ID=bridge-github
MEP_TARGET_NODE_ID=node_123
MEP_TARGET_ALIAS=Hub Sentinel
MEP_BRIDGE_STATUS_SECRET=replace-me
MEP_BRIDGE_DEDUP_TTL_HOURS=72
MEP_BRIDGE_COALESCE_WINDOW_SECONDS=10
MEP_BRIDGE_COALESCE_MAX_BUFFER_SIZE=50
# reserved follow-up config: documented staleness policy is approved, but bridge-side enforcement is not implemented in this PR
MEP_BRIDGE_STALE_DM_SOFT_TTL_SECONDS=7200
MEP_BRIDGE_STALE_DM_HARD_TTL_SECONDS=86400

GITHUB_WEBHOOK_SECRET=replace-me
GITHUB_PAT=replace-me
GITHUB_ALLOWED_REPOS=WUAIBING/MEP

TELEGRAM_BOT_TOKEN=replace-me
TELEGRAM_CHAT_ID=replace-me

MEP_LIVE_CALL_ENABLED=true
MEP_DM_TO_CALL_BRIDGE_ENABLED=true
MEP_CALL_AUTO_ACCEPT=true
```

### Bot Owner Steps

1. Deploy or update the MEP Hub.
2. Ensure the target bot runtime is online.
3. Set the bridge environment variables.
4. Start the bridge service.
5. Confirm the bridge can reach MEP Hub.
6. Confirm the target node ID is correct.
7. Confirm Telegram notification works.
8. Trigger a GitHub test webhook.
9. Verify that the event becomes a targeted MEP task, not just a Telegram notification.

## GitHub Webhook Setup Guide

### Repository Owner Steps

1. Open the GitHub repository.
2. Go to `Settings -> Webhooks`.
3. Click `Add webhook`.
4. Set the payload URL to `${MEP_BRIDGE_PUBLIC_BASE_URL}/github/webhook`.
5. Set content type to `application/json`.
6. Set the shared secret to the configured `GITHUB_WEBHOOK_SECRET`.
7. Select events:
   - `Pull requests`
   - `Issue comments`
   - `Pull request review comments`
   - optionally `Issues`
8. Save the webhook.
9. Use GitHub's redelivery test to confirm the bridge returns success.

### Validation Expectations

The bridge must:

- reject invalid signatures
- reject unsupported repos if allowlisted
- return clear non-2xx errors for invalid requests
- return 2xx only when the event is accepted and processed or intentionally ignored

### Replay Protection

Replay protection must be persisted, not in-memory only.

Recommended rule:

- persist `delivery_id -> bridge_id, timestamp, status`
- retain for at least 72 hours
- survive bridge restarts

This prevents duplicate GitHub redelivery from causing duplicate reviews.

### Event Coalescence

GitHub frequently emits multiple related events for the same PR within seconds.

Recommended first-slice rule:

- buffer events by `context_id` for 5-10 seconds
- deduplicate or merge rapid-fire events
- submit one consolidated actionable task where possible
- cap the coalescence buffer to a bounded size and flush oldest buffered contexts when the cap is exceeded

Example burst:

- PR opened
- synchronize after force-push
- issue comment mention
- review comment mention

These should not necessarily produce four independent autonomous reviews.

## Execution Behavior

### Online Target

If the target bot is online:

- bridge submits targeted task
- hub routes `new_task`
- runtime processes task immediately
- runtime may upgrade to `call.*`

The bridge should not trust registry presence alone as proof that delivery succeeded.

If the target appears online but live delivery cannot be confirmed, the bridge must treat the node as a possible ghost node and fall back to queue-safe behavior where appropriate.

### Offline Target

If the target bot is offline:

- bridge still submits targeted task
- hub queues the zero-bounty DM
- Telegram status says queued, not failed
- bot processes it when it reconnects

This gives voicemail semantics instead of dropping the event.

### Staleness Policy For Queued DMs

Queued autonomous work must not execute indefinitely without freshness checks.

Recommended first-slice policy:

- if the queued item is older than 2 hours and the PR or Issue is already closed or merged, auto-skip and notify Telegram
- if the queued item is older than 24 hours, do not auto-process; notify the operator and require a fresh trigger or explicit policy override

This prevents a reconnecting bot from acting on stale review requests.

Note: this policy is part of the approved design, but the current PR implements the bridge-side core only. TTL enforcement remains a follow-up change on top of the shipped bridge persistence and status plumbing.

## Failure Handling

### Signature Failure

- reject request
- do not submit to MEP
- optionally log local warning only

### Unsupported Event

- accept but ignore
- optional Telegram debug message in verbose mode only

### MEP Submission Failure

- report Telegram failure
- include repo, number, and reason
- keep enough metadata for retry
- preserve `bridge_id` for later retry or operator diagnosis

### Replay Or Duplicate Delivery

- detect duplicate `delivery_id`
- return the persisted bridge outcome when safe
- never create a second autonomous review for the same deduplicated delivery unless policy explicitly replays it

### Target Not Routed

- report configuration error
- do not silently drop the event

### Bot Requires Human Approval

- send Telegram status
- preserve `context_id`
- preserve `bridge_id`
- do not lose thread identity

### Bridge Status Failure

- reject unauthenticated status requests
- reject requests without `bridge_id`
- accept idempotent retried status updates for the same `bridge_id`
- report correlation failures explicitly instead of silently dropping them

### Ghost Node Delivery Failure

- if a node appears online but the bridge cannot confirm live routing or the hub falls back, preserve queue-safe semantics
- record the incident as ghost-node or stale-registration behavior for diagnosis

## Observability

The bridge must log:

- webhook received
- signature verified
- event normalized
- actionable decision
- coalescence decision
- target resolved
- MEP submission result
- Telegram notification result
- bridge status callback received
- final workflow outcome if known

Recommended structured fields:

- `source_type`
- `delivery_id`
- `bridge_id`
- `event_sequence`
- `repo_full_name`
- `kind`
- `number`
- `context_id`
- `target_node_id`
- `actionable`
- `submission_status`

## Security

### Required

- verify GitHub webhook signature
- authenticate `POST /bridge/status`
- allowlist repositories where needed
- do not trust mention text alone without repo policy
- bind status tokens to bridge execution or trusted bot identity with expiry
- keep PAT and webhook secret out of logs
- send only sanitized excerpts to Telegram

### Recommended

- use GitHub App auth later instead of broad PAT
- add replay protection keyed by GitHub delivery ID
- make bridge status updates idempotent by `bridge_id` plus status phase or status event ID
- prefer persisted replay protection with at least 72h retention
- add trigger actor permission checks for sensitive repos
- add rate limits per repo

## Implementation Shape

### First PR Scope

Build:

- GitHub webhook endpoint or bridge worker receiver
- event normalizer
- target resolver
- MEP structured DM builder
- bridge correlation store keyed by `bridge_id`
- persisted replay-protection store keyed by `delivery_id`
- coalescence buffer keyed by `context_id`
- coalescence max-buffer guard to prevent unbounded buffered contexts under burst load
- targeted submission to hub
- authenticated `/bridge/status` endpoint
- Telegram status notifier
- focused tests
- deployment docs

Do not build yet:

- Discord adapter
- Slack adapter
- full bidirectional IM reply system
- large UI/dashboard

## File Targets

Suggested first-slice targets:

- `docs/external-bridge/GITHUB_TO_MEP_TELEGRAM_FIRST_SLICE.md`
- `hub/main.py` or new bridge module entrypoint
- new bridge normalization module
- new bridge config module
- new bridge Telegram notifier module
- focused tests under `tests/`

If a standalone worker is preferred, use a dedicated module path rather than hardwiring all bridge logic into the hub.

## Acceptance Criteria

This slice is successful when:

1. a GitHub mention event is accepted and verified
2. the bridge resolves the correct target node
3. the bridge submits a targeted `mep.interbot.v1` task to MEP Hub
4. the target bot receives the event as `new_task`
5. Telegram shows visible status updates
6. the existing runtime can optionally upgrade the thread into `call.*`
7. offline targets are queued instead of silently dropped
8. bridge status updates correlate back to the original request with `bridge_id`
9. stale queued work is skipped or escalated according to policy
10. rapid-fire webhook bursts are coalesced into safe actionable work
11. failures are explicit and diagnosable

## Minimal Test Plan

Add focused tests for:

- valid webhook signature -> event accepted
- invalid webhook signature -> rejected
- PR mention -> actionable structured DM submitted
- non-actionable event -> ignored cleanly
- non-imperative mention -> notify-only or ignored according to policy
- target online -> `new_task` delivered
- target offline -> queued DM behavior
- ghost node appears online but cannot receive -> queue-safe handling
- queued DM older than soft TTL and closed PR -> auto-skip
- queued DM older than hard TTL -> operator-visible escalation
- rapid webhook burst on same PR -> coalesced submission
- Telegram status notification emitted
- stable `context_id` for repeated events on same PR
- increasing `event_sequence` for same thread
- `bridge_id` present in inbound DM payload
- authenticated `POST /bridge/status` accepted and correlated
- duplicate status update handled idempotently
- duplicate `delivery_id` handled from persisted dedup state

## Numeric Safety Note

This first slice uses `bounty_ns = 0` with `currency = "MEP_NS"` for targeted bridge-triggered DM by default.

If future bridge policy introduces computed bounty values, use precise decimal handling rather than raw binary float arithmetic.

Recommended pattern:

- parse external numeric input as decimal-safe strings
- convert using `Decimal(str(value))`
- convert to integer nanoseconds at the final protocol boundary

## Recommended Next PR Title

```text
feat(bridge): add GitHub-to-MEP bridge with Telegram status updates
```

## Final Note

The correct first product slice is not "GitHub to Telegram only".

The correct first slice is:

- GitHub as the external source
- MEP as the action engine
- Telegram as the status surface

That keeps the architecture clean, makes Hub Sentinel actually autonomous, and creates a reusable bridge pattern for future IM platforms.
