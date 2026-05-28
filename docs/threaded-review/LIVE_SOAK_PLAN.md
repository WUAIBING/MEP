# Live Soak Plan

Use this plan for the real staged live soak with named participants. Keep `SOAK_RUNBOOK.md` as the reusable universal operator playbook; use this file when the Human Governor wants the concrete execution sequence for a specific live session.

## Roles

- `Human Governor`: you
- `Organizer / operator-side technical bot`: the bot coordinating the soak, driving the command flow, and reporting summaries back to the Human Governor
- `Reviewer A`: first substantive reviewer bot
- `Reviewer B`: second substantive reviewer bot
- `Observer`: process and safety observer bot
- `Reserve nodes`: optional additional reviewers that stay out of the first live run unless the Human Governor explicitly expands the session

## Recommended Round 1 Participants

- `Human Governor`: human final authority
- `Organizer`: operator-side execution bot that knows the guarded workflow and recent code changes
- `Claude Code bot`: reviewer A
- `OpenCode bot`: reviewer B
- `Hub Sentinel`: observer
- `Elsaws`, `Qoder bot`, `Workbuddy bot`: reserve nodes for later rounds after the first live soak passes

Keep the first live soak small enough to diagnose. Add reserve nodes only after the first guarded session proves stable.

## Stage 0: Node Readiness

Before any preflight thread starts, the Human Governor tells every participating node to:

- connect to the same `HUB_URL` and `WS_URL`
- stay online for the full test window
- run a real inference-capable adapter or runtime
- avoid ack-only replies such as `ack`, `received`, or `continuing`
- answer review prompts with judgment, rationale, and a next step

Minimum readiness checklist for each reviewer node:

- valid node identity key
- correct hub environment variables
- intended adapter launched and registered
- AI inference path working
- understands that the session is a guarded threaded review, not a transport ping test

## Stage 1: Readiness Confirmation

Before the guarded preflight thread, confirm:

- each active node is registered on the same hub
- the intended reviewer node IDs are known
- the Human Governor target is reachable if final approval is part of the run
- the organizer bot can launch the required stdio adapter commands

If any active reviewer is offline or not inference-ready, do not continue to the preflight thread.

## Stage 2: Preflight

Run one short guarded mini-thread that cannot be passed by ack behavior.

Suggested preflight prompt:

```text
Preflight review: identify one risk in running a guarded multi-bot soak and recommend one mitigation.
```

Suggested preflight start command:

```text
mepdmx <reviewer_node_id> "Preflight review: identify one risk in running a guarded multi-bot soak and recommend one mitigation." --context <preflight_context_id> --turn-type review_request --intent review.request --priority high --max-turns 4 --max-duration-seconds 600 --checkpoint-interval 2
```

Then run:

```text
mepdmlist --context <preflight_context_id> --limit 5
mepdmsnapshot --context <preflight_context_id> --label start --limit 5
mepdmreplysafe --context <preflight_context_id> auto "Good. Now name the most likely operator mistake in this workflow and how to avoid it." --turn-type review_response --intent review.response
mepdmlist --context <preflight_context_id> --limit 5
mepdmsnapshot --context <preflight_context_id> --label end --limit 5
```

## Stage 3: Go / No-Go Gate

After preflight, the organizer summarizes:

- which nodes were online
- which nodes produced substantive AI-generated replies
- whether the thread stayed on one `context_id`
- whether `mepdmlist --context`, `mepdmsnapshot`, and `mepdmreplysafe --context ... auto ...` worked
- whether any node fell back to ack-style behavior

The organizer then makes an explicit recommendation:

- `GO`: continue to the one-hour soak
- `NO-GO`: stop and fix readiness or workflow issues first

The Human Governor decides whether the one-hour soak begins.

## Stage 4: Real One-Hour Soak

Only start this stage after an explicit `GO`.

Use the universal commands and evidence flow from `SOAK_RUNBOOK.md`, but keep this real-world participant model:

- one Human Governor
- one organizer / operator-side technical bot
- two active reviewer bots
- one observer
- reserve bots kept out of the first live thread unless explicitly promoted

Use these defaults:

- `--max-turns 12`
- `--max-duration-seconds 3600`
- `--checkpoint-interval 3`

Capture:

- one start snapshot
- one midpoint snapshot
- one end snapshot
- at least one checkpoint line
- the final human handoff or clean stop line

## Stage 5: Stop And Final Summary

When the soak ends, the organizer reports to the Human Governor:

- whether the session stayed inside one guarded thread
- whether the nodes remained substantive instead of reverting to ack-style behavior
- whether checkpointing and bounded replies worked
- whether the evidence bundle is complete
- what broke, if anything
- whether to repeat the same topology, expand to reserve nodes, or fix issues before another run

## Pass Conditions

- all active participants stay on the same hub
- reviewer nodes produce substantive AI-generated reasoning
- the operator uses context-scoped commands instead of rebuilding thread metadata by hand
- preflight passes and the Human Governor explicitly says `GO`
- the one-hour soak ends with a clean human approval handoff or a clean guarded stop

## Related References

- `SOAK_RUNBOOK.md`
- `OPERATOR_CHECKLIST.md`
- `scripts/threaded_review_example.py`
