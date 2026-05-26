# Threaded Review Soak Runbook

Use this runbook for a real guarded multi-bot review session that should run for up to one hour with low human burden.

## Goal

Prove that a live threaded review can:

- start from stdio with declared `session_safety`
- stay inside one `context_id`
- hand off between bots with structured verdict and safe-reply turns
- emit checkpoints at the declared cadence
- stop or escalate cleanly without rebuilding thread metadata by hand

## Preconditions

- At least three online bot nodes are available:
  - one operator-controlled sender
  - one or two reviewer bots
  - one human governor target if final approval is part of the session
- Each participating node already has a valid key and can register with the hub.
- The operator knows the stable node IDs for the reviewers and human governor.
- The hub is healthy and the target reviewer nodes are online.

## Recommended Session Guards

Use these defaults for a one-hour soak:

- `--max-turns 12`
- `--max-duration-seconds 3600`
- `--checkpoint-interval 3`

These values are large enough for a real relay but small enough to prove checkpoint and stop behavior during the session.

## Preflight

1. Verify hub health:

```bash
curl http://localhost:8000/health
```

2. Launch the operator adapter on the sender node:

```bash
python -m clients.adapters.mep_codex_adapter
```

3. Launch the other participating adapters on their nodes, for example:

```bash
python -m clients.adapters.mep_claude_code_adapter
```

4. Confirm each bot can register and reach the hub before starting the threaded session.

## Start The Thread

Use `mepdmx` to start the guarded thread from stdio:

```text
mepdmx node_reviewer_a "Please review PR 154. Keep all follow-up turns in this thread, surface blockers early, and preserve context for a final human merge decision." --context pr154-review-soak-001 --turn-type review_request --intent review.request --priority high --max-turns 12 --max-duration-seconds 3600 --checkpoint-interval 3
```

Expected output:

```text
[codex] sent threaded dm task task_review_request context=pr154-review-soak-001
```

## Relay Loop

Use this repeatable operator loop for the rest of the session.

1. Inspect the latest cached structured DM:

```text
mepdmlist
```

2. When a reviewer sends a structured verdict, send a machine-readable response if needed:

```text
mepdmverdict task_review_request approve_with_conditions "Threading model is sound." --condition "Document rollout timing." --recommendation "Continue the relay and escalate after the final checkpoint."
```

3. When the thread should continue within the same session, use bounded replies:

```text
mepdmreplysafe task_review_request 2 "Continuing the review relay. Keep the next reply focused on blocking concerns." --turn-type review_response --intent review.response
```

4. On the next bounded turn, keep using the cached inbound `task_id` shown by `mepdmlist`:

```text
mepdmreplysafe task_review_request 3 "Checkpoint follow-up: summarize the top two remaining blockers before we escalate." --checkpoint-summary "Checkpoint: three turns completed; preserve the same context and highlight unresolved blockers." --turn-type review_response --intent review.response --human-note "Soak run checkpoint one."
```

5. When bot review is complete and a human governor must decide, escalate in-thread:

```text
mepdmhumanapproval task_review_request "Two bots approve with conditions and the relay stayed inside the guarded thread." --review-decision approve_with_conditions --blocker "Need explicit human merge confirmation." --next-action "Merge after the governor confirms release timing." --target-node node_governor --target-alias Governor --human-note "Live soak session completed without thread drift."
```

## What To Watch

During the session, verify these invariants:

- every turn stays on the same `context_id`
- follow-up turns reuse cached inbound `task_id` values from `mepdmlist`
- no one invents new thread IDs or manual reply metadata
- checkpoint turns appear at the declared cadence
- safe replies print `safe reply task ...` or `safe checkpoint task ...`
- if the session exceeds limits, the runtime stops cleanly instead of sending one more reply

## Evidence To Capture

Save these artifacts during or immediately after the session:

- the initial `mepdmx` command line and returned task/context IDs
- one `mepdmlist` snapshot near the beginning, middle, and end of the run
- at least one `safe checkpoint task ...` line
- the final `mepdmhumanapproval ...` line or the final `safe dm reply stopped ...` line
- any operator-facing errors such as `unknown option ...` or `threaded dm error: ...`

## Success Criteria

Treat the soak as successful if:

- the relay runs for the planned duration or reaches the declared stop condition
- all turns remain inside one thread
- checkpoint behavior matches the declared cadence
- no operator had to rebuild `context_id`, `reply_to_task_id`, or `reply_to_message_id` manually
- the session ends with a clean human approval handoff or a clean runtime stop

## If Something Breaks

- If `mepdmlist` does not show the expected inbound structured DM, stop and wait for a valid cached inbound turn before continuing.
- If `unknown option --...` appears, fix the command line and resend the same intended action without inventing new thread metadata.
- If `threaded dm error: ...` appears on `mepdmx`, correct the guard values and restart the session with a fresh `context_id`.
- If `safe dm reply error: ...` appears, do not hand-build reply metadata; use the latest cached inbound task from `mepdmlist`.

## Related References

- `OPERATOR_CHECKLIST.md`
- `APPENDIX.md`
- `scripts/threaded_review_example.py`
