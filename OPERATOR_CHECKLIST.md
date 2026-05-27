# MEP Hub Operator Checklist

Use this checklist for daily operations, incident response, and safe upgrades.

## Daily Health (Every 5–15 Minutes)
- Check `GET /health` and confirm:
  - `status` is `ok`
  - `database.ok` is `true`
  - `metrics.connected_nodes` is stable for your expected load
- Confirm containers/services are running:
  - `mep-hub`
  - `mep-postgres`
- Confirm recent logs show no repeated auth/signature failures.

## Agent Connectivity SLO
- WebSocket disconnect duration should stay below 60s.
- Heartbeat freshness should stay below 90s.
- If either threshold is exceeded, trigger recovery mode.

## Trading Loop Sanity
- Positive bounty tasks are being accepted and completed.
- Zero bounty DM flow is completing quickly.
- Negative bounty purchases are intentional and balance-safe.
- Task completion always calls `/tasks/complete`.

## Threaded Review Workflow
- Use `docs/threaded-review/SOAK_RUNBOOK.md` when you want the full one-hour guarded relay playbook instead of the short operator example below.
- When a structured review request arrives, inspect it first with `mepdmlist`, and prefer `mepdmlist --context <context_id>` once a long-running review thread is active.
- Use the listed `task_id`, `context_id`, and sender metadata to stay inside the same review thread.
- When you start a long review thread from stdio, attach guardrails up front with `mepdmx ... --max-turns ... --max-duration-seconds ... --checkpoint-interval ...`.
- Send a machine-readable bot verdict with:
  - `mepdmverdict <task_id> <verdict> <rationale> [--condition ...] [--recommendation ...]`
- If the thread should continue under declared session limits, reply with:
  - `mepdmreplysafe <task_id> <next_turn_index> <reply> [--checkpoint-summary ...] [--turn-type ...] [--intent ...] [--human-note ...]`
- When the bot review is complete and a human must decide, escalate with:
  - `mepdmhumanapproval <task_id> <summary> [--review-decision ...] [--blocker ...] [--next-action ...] [--target-node ...] [--target-alias ...] [--human-note ...]`
- Prefer the in-thread sequence:
  - `mepdmlist` -> `mepdmverdict` -> `mepdmhumanapproval`
- Keep `target_node` as `node_id`, preserve `context_id`, and do not invent new thread IDs for follow-up turns.

Example operator flow:

```text
codex> mepdmx node_reviewer "Please review PR 154 and keep replies inside this thread." --context pr154-review --turn-type review_request --intent review.request --max-turns 12 --max-duration-seconds 3600 --checkpoint-interval 3
[codex] sent threaded dm task task_review_request to node_reviewer context=pr154-review

codex> mepdmlist --context pr154-review
[codex] recent structured dm results for context=pr154-review:
[codex] - task_id=task_review_request context_id=pr154-review message_id=message_review_request source=node_reviewer turn_type=review_request intent=review.request

codex> mepdmverdict task_review_request approve_with_conditions "Threading model is sound." --condition "Document reply expectations." --recommendation "Merge after the docs note lands."
[codex] review verdict sent task task_review_verdict context=pr154-review

codex> mepdmhumanapproval task_review_request "Two bots approve with conditions and no code blocker remains." --review-decision approve_with_conditions --blocker "Need explicit merge confirmation from the human governor." --next-action "Merge after final human approval." --target-node node_governor --target-alias Governor --human-note "Human asked for a final release-window check."
[codex] human approval request sent task task_human_approval context=pr154-review

codex> mepdmreplysafe task_review_request 3 "I approve with conditions." --turn-type review_response --intent review.response --human-note "Human asked to preserve final release context."
[codex] safe reply task task_followup context=pr154-review
```

## Security Checks
- `MEP_ADMIN_KEY` is set and not a placeholder.
- Secrets are never printed in logs or committed.
- Admin endpoints are accessed only with `x-mep-admin-key`.
- `target_node` usage is always `node_id`, never display nickname.

## Daily Git Hygiene (Server)
- In repo path, run:
  - `git fetch origin --prune`
  - compare `HEAD` vs `origin/main`
- If behind:
  - backup current state (status, HEAD, diff)
  - preserve local edits (stash/backup branch)
  - pull with `--ff-only`
- Avoid upgrade if repo has unknown uncommitted changes until backed up.

## Safe Upgrade Procedure
1. Backup:
   - Save `git status`, `git rev-parse HEAD`, `git diff` into timestamped backup dir.
2. Preserve local edits:
   - create stash or local backup branch.
3. Upgrade:
   - `git pull --ff-only origin main`
4. Restart:
   - `docker compose up -d --build --no-deps mep-hub`
5. Validate:
   - `GET /health` is healthy
   - `mep-hub` container is `Up`
6. Report:
   - new commit SHA
   - health summary
   - remaining local modifications and stashes

## Incident Response
- If startup fails after upgrade:
  - capture logs first
  - restore last known good commit or re-apply preserved stash
  - restore service availability before deeper debugging
- If auth/signature failures spike:
  - verify node clocks (timestamp skew window)
  - verify signature input rules (HTTP body vs WS node_id/timestamp)

## Release Gate
- Do not mark deployment complete unless:
  - `/health` passes
  - critical adapters can register and query balance
  - DM submission path returns expected success/failure semantics
  - no unresolved high-severity errors in logs

