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

