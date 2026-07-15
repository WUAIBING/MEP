[RESOLVED] approve-callback-loss

## Symptom
- `@Hub-Sentinel approve this PR` reaches bridge and creates a `bridge_executions` row.
- Live execution stays at submission-only `status=success` with empty `action` and empty `review_result_json`.
- No final GitHub review artifact is published.

## Expected
- Approve flow must produce a final bridge status callback and GitHub writeback.
- If CI is non-green, it should publish a visible `reviewed` blocker.
- If CI is green, it should publish the final review outcome.

## Current Evidence
- Candidate bridge on DO runs commit `c962af4`.
- Latest live execution: `br-3e07687f10bfee62`, `intent_type=code.review.approve`, `status=success`, no final action/result.
- DO has runtime drift: managed `mep-hub-sentinel` plus rogue `Hub Sentinel` processes.

## Falsifiable Hypotheses
1. The `approve` task is accepted by bridge but never consumed by the active Hub Sentinel runtime.
2. The active runtime consumes the task but exits before posting `/bridge/status`.
3. `/bridge/status` is attempted but rejected due to token/config mismatch after deployment drift.
4. A rogue runtime process consumes the task and runs with stale config, so final callback goes nowhere or is skipped.
5. The runtime completes `approve` intent through a code path that omits bridge status reporting entirely.

## Plan
1. Instrument runtime bridge-status emission and approve task completion path.
2. Reproduce with a fresh live approve trigger.
3. Compare runtime logs and bridge logs to isolate where callback is lost.
4. Apply the minimal fix only after evidence confirms the failing hop.

## Evidence
- Bridge routing on DO targeted `node_b2f19654a37c`, while the managed `mep-hub-sentinel.service` was registering a different node id from `/root/mep-hub/MEP/node/sentinel.pem`.
- The active reviewer process was a rogue `python3 -u -m node.mep_runtime ... --key-path /root/openclaw/workspace/mep-sentinel/node/sentinel.pem --adapter deepseek run --alias Hub Sentinel` outside `systemd`.
- After moving that exact runtime command under `mep-hub-sentinel.service`, runtime logs showed `completed task=...` followed by `bridge status reported task=... bridge_id=...`.
- Bridge logs then showed `POST /bridge/status HTTP/1.1 200 OK`, proving the missing callback hop was restored.
- A second live issue remained after callback recovery: bridge still suppressed approved writeback with `verified_identifiers_in_context_only` even when the identifier token was present in the changed patch.

## Root Cause
- Primary root cause: production reviewer ownership drift. GitHub bridge routed approve tasks to a rogue `Hub Sentinel` node id that was not owned by `systemd`, so live work bypassed the managed, instrumented path.
- Secondary root cause: bridge classified `Changed identifiers verified` entries by raw string membership against `changed_text`; backticks and trailing punctuation caused false `verified_identifiers_in_context_only` suppression.
- Runtime also over-trusted `risk_pack.changed_identifiers` as publishable evidence, even when an identifier was too long or not visible in the compact patch excerpts sent to the reviewer.

## Fix
- Repointed `mep-hub-sentinel.service` to run the real reviewer runtime from the clean deployed worktree and the bridge-targeted key `/root/openclaw/workspace/mep-sentinel/node/sentinel.pem`.
- Removed dependence on the rogue reviewer process so `systemd` owns the active `Hub Sentinel` reviewer path.
- Hardened `node/mep_runtime.py` to keep only exact, renderable identifiers and to drop identifiers not visible in the reviewer patch excerpts.
- Hardened `bridge/github_to_mep.py` to classify `Changed identifiers verified` by extracted identifier tokens instead of raw section strings.

## Verification
- Local focused runtime tests passed for:
  - overlong identifiers are dropped instead of clipped
  - non-renderable identifiers are removed
  - identifiers absent from patch excerpts are removed before publication
- Local focused bridge tests passed for:
  - context-only verified identifiers still suppress
  - backticked identifiers with trailing punctuation no longer false-suppress
- Live proof on `PR #313` succeeded:
  - bridge row `br-a4b33c7b335b2bd1` reached `status=completed`, `action=approved`
  - `review_result_json` recorded `published=true`, `suppressed=false`
  - GitHub recorded a fresh `Hub-Sentinel` `APPROVED` review with `<!-- mep-bridge:output bridge_id=br-a4b33c7b335b2bd1 action=approved -->`
