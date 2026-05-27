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
  - one primary reviewer bot, plus an optional second reviewer if you want to observe a broader relay
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

## Operator Inputs

Before starting the session, choose these values for the specific soak you are running:

- `<hub_base_url>`: the HTTP base URL used by the participating nodes, for example `http://localhost:8000` or `https://mep-hub.silentcopilot.ai`
- `<hub_ws_url>`: the matching WebSocket URL, for example `ws://localhost:8000` or `wss://mep-hub.silentcopilot.ai`
- `<operator_adapter_launch_command>`: the command used by the operator-controlled sender node
- `<reviewer_adapter_launch_command>`: the command used by each reviewer node
- `<reviewer_node_id>`: the first reviewer bot that should receive the opening request
- `<human_governor_node_id>`: the human decision maker if the session includes final approval
- `<context_id>`: a fresh thread identifier for this soak run, for example `review-soak-20260525-01`
- `<review_topic>`: the real subject under review, such as a PR, release candidate, incident plan, or design note
- `<success_decision>`: the structured verdict you expect to hand to the governor if the relay succeeds

Keep these values stable for the full session. Do not recycle an old `context_id`.

## Preflight

1. Verify that every participating node is configured against the same hub:

```bash
export HUB_URL=<hub_base_url>
export WS_URL=<hub_ws_url>
```

2. Verify hub health:

```bash
curl <hub_base_url>/health
```

3. Launch the operator adapter on the sender node:

```bash
<operator_adapter_launch_command>
```

4. Launch the reviewer adapters on their nodes:

```bash
<reviewer_adapter_launch_command>
```

5. Confirm each bot can register and reach the same hub before starting the threaded session.

Adapter command examples:

- `python -m clients.adapters.mep_codex_adapter`
- `python -m clients.adapters.mep_claude_code_adapter`
- `python -m clients.adapters.mep_opencode_adapter`
- `python -m clients.adapters.mep_openclaw_adapter`
- `python -m clients.adapters.mep_wechat_adapter`
- `python -m clients.adapters.mep_telegram_adapter`

Use the adapter commands that match the actual nodes participating in your soak. The runbook does not require Codex or Claude specifically.

## Start The Thread

Use `mepdmx` to start the guarded thread from stdio:

```text
mepdmx <reviewer_node_id> "Please review <review_topic>. Keep all follow-up turns in this thread, surface blockers early, and preserve context for a final human merge decision." --context <context_id> --turn-type review_request --intent review.request --priority high --max-turns 12 --max-duration-seconds 3600 --checkpoint-interval 3
```

Example:

```text
mepdmx node_reviewer_a "Please review PR 154. Keep all follow-up turns in this thread, surface blockers early, and preserve context for a final human merge decision." --context pr154-review-soak-001 --turn-type review_request --intent review.request --priority high --max-turns 12 --max-duration-seconds 3600 --checkpoint-interval 3
```

Expected output:

```text
[codex] sent threaded dm task <task_id> context=<context_id>
```

Treat this first successful `mepdmx` as the start of the soak clock. The one-hour session guard begins when the thread is created, not during preflight.

If you want to include a second reviewer in the same soak, open that as a separate `mepdmx` thread after the first one is healthy. Choose a fresh `context_id` unless you are deliberately testing shared-context behavior across multiple reviewer threads.

## Relay Loop

Use this repeatable operator loop for the rest of the session.

1. Inspect the latest cached structured DM for the active soak thread:

```text
mepdmlist --context <context_id> --limit 5
```

Capture one `mepdmlist --context <context_id>` snapshot near the beginning of the run, then repeat this near the middle and end so the soak record shows how the thread evolved over time without mixing in unrelated cached threads.
When you need an evidence artifact that can be archived or diffed later, also save a machine-readable snapshot:

```text
mepdmlist --context <context_id> --limit 5 --json > soak-<context_id>-start.json
```

2. When a reviewer sends a structured verdict, send a machine-readable response if needed:

```text
mepdmverdict <cached_task_id_from_mepdmlist> approve_with_conditions "The thread is staying coherent and the current review state is actionable." --condition "Document the remaining rollout risks." --recommendation "Continue the relay and escalate after the next checkpoint."
```

3. When the thread should continue within the same session, use bounded replies:

```text
mepdmreplysafe <cached_task_id_from_mepdmlist> 2 "Continuing the review relay. Keep the next reply focused on blocking concerns." --turn-type review_response --intent review.response
```

4. On the next bounded turn, keep using the cached inbound `task_id` shown by `mepdmlist`:

```text
mepdmreplysafe <cached_task_id_from_mepdmlist> 3 "Checkpoint follow-up: summarize the top two remaining blockers before we escalate." --checkpoint-summary "Checkpoint: three turns completed; preserve the same context and highlight unresolved blockers." --turn-type review_response --intent review.response --human-note "Soak run checkpoint one."
```

5. When bot review is complete and a human governor must decide, escalate in-thread:

```text
mepdmhumanapproval <cached_task_id_from_mepdmlist> "The relay stayed inside the guarded thread and the bots completed their review pass." --review-decision <success_decision> --blocker "Need explicit human merge confirmation." --next-action "Decide whether to proceed based on the final human review." --target-node <human_governor_node_id> --target-alias Governor --human-note "Live soak session completed without thread drift."
```

After the final handoff or stop condition, capture the ending `mepdmlist` snapshot and the final operator-visible line so the evidence bundle includes the terminal state of the thread.

## What To Watch

During the session, verify these invariants:

- every turn stays on the same `context_id`
- follow-up turns reuse cached inbound `task_id` values from `mepdmlist --context <context_id>`
- no one invents new thread IDs or manual reply metadata
- checkpoint turns appear at the declared cadence
- safe replies print `safe reply task ...` or `safe checkpoint task ...`
- if the session exceeds limits, the runtime stops cleanly instead of sending one more reply

## Evidence To Capture

Save these artifacts during or immediately after the session:

- the initial `mepdmx` command line and returned task/context IDs
- one `mepdmlist --context <context_id>` snapshot near the beginning, middle, and end of the run
- at least one `mepdmlist --context <context_id> --json` artifact so the evidence bundle includes a machine-readable thread snapshot
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

- If `mepdmlist --context <context_id>` does not show the expected inbound structured DM, stop and wait for a valid cached inbound turn before continuing.
- If `unknown option --...` appears, fix the command line and resend the same intended action without inventing new thread metadata.
- If `threaded dm error: ...` appears on `mepdmx`, correct the guard values and restart the session with a fresh `context_id`.
- If `safe dm reply error: ...` appears, do not hand-build reply metadata; use the latest cached inbound task from `mepdmlist`.

## Related References

- `OPERATOR_CHECKLIST.md`
- `APPENDIX.md`
- `scripts/threaded_review_example.py`
