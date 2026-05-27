import asyncio
import os
from typing import Optional

from clients.shared.mep_client import MEPClient


TARGET_NODE = os.getenv("MEP_TARGET_NODE", "").strip()
TARGET_ALIAS = os.getenv("MEP_TARGET_ALIAS", "").strip() or None
HUMAN_TARGET_NODE = os.getenv("MEP_HUMAN_TARGET_NODE", "").strip() or None
HUMAN_TARGET_ALIAS = os.getenv("MEP_HUMAN_TARGET_ALIAS", "").strip() or None
KEY_PATH = os.getenv("MEP_BOT_KEY_PATH", "").strip()
CONTEXT_ID = os.getenv("MEP_CONTEXT_ID", "example-threaded-review-001").strip()
REPLY_TO_TASK_ID = os.getenv("MEP_REPLY_TO_TASK_ID", "").strip() or None
REPLY_TO_MESSAGE_ID = os.getenv("MEP_REPLY_TO_MESSAGE_ID", "").strip() or None
MAX_TURNS = int(os.getenv("MEP_SESSION_MAX_TURNS", "6"))
MAX_DURATION_SECONDS = int(os.getenv("MEP_SESSION_MAX_DURATION_SECONDS", "900"))
CHECKPOINT_INTERVAL = int(os.getenv("MEP_SESSION_CHECKPOINT_INTERVAL", "3"))
SNAPSHOT_LIMIT = int(os.getenv("MEP_SOAK_SNAPSHOT_LIMIT", "5"))
REVIEW_REQUEST = os.getenv(
    "MEP_REVIEW_REQUEST",
    (
        "Please review the current change. Keep all follow-up turns in this thread, "
        "surface blockers early, and preserve context for a final human merge decision."
    ),
).strip()


def _quote_stdio_text(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_stdio_soak_plan(
    context_id: str,
    *,
    snapshot_limit: int = 5,
    human_target_node: Optional[str] = None,
    human_target_alias: Optional[str] = None,
) -> list[tuple[str, str]]:
    if snapshot_limit <= 0:
        raise ValueError("snapshot_limit must be a positive integer")

    plan = [
        ("Inspect cached thread state", f"mepdmlist --context {context_id} --limit {snapshot_limit}"),
        ("Write start evidence snapshot", f"mepdmsnapshot --context {context_id} --label start --limit {snapshot_limit}"),
        (
            "Send a structured review verdict",
            " ".join(
                [
                    f"mepdmverdict --context {context_id} approve_with_conditions",
                    _quote_stdio_text("The thread is staying coherent and the current review state is actionable."),
                    "--condition",
                    _quote_stdio_text("Document the remaining rollout risks."),
                    "--recommendation",
                    _quote_stdio_text("Continue the relay and escalate after the next checkpoint."),
                ]
            ),
        ),
        (
            "Continue the guarded relay",
            " ".join(
                [
                    f"mepdmreplysafe --context {context_id} auto",
                    _quote_stdio_text("Continuing the review relay. Keep the next reply focused on blocking concerns."),
                    "--turn-type review_response --intent review.response",
                ]
            ),
        ),
        (
            "Capture a midpoint evidence snapshot",
            f"mepdmsnapshot --context {context_id} --label mid --limit {snapshot_limit}",
        ),
        (
            "Send the next bounded checkpoint follow-up",
            " ".join(
                [
                    f"mepdmreplysafe --context {context_id} auto",
                    _quote_stdio_text("Checkpoint follow-up: summarize the top two remaining blockers before we escalate."),
                    "--checkpoint-summary",
                    _quote_stdio_text(
                        "Checkpoint: three turns completed; preserve the same context and highlight unresolved blockers."
                    ),
                    "--turn-type review_response --intent review.response --human-note",
                    _quote_stdio_text("Soak run checkpoint one."),
                ]
            ),
        ),
    ]

    if human_target_node:
        approval_parts = [
            f"mepdmhumanapproval --context {context_id}",
            _quote_stdio_text("The relay stayed inside the guarded thread and the bots completed their review pass."),
            "--review-decision approve_with_conditions --blocker",
            _quote_stdio_text("Need explicit human merge confirmation."),
            "--next-action",
            _quote_stdio_text("Decide whether to proceed based on the final human review."),
            f"--target-node {human_target_node}",
        ]
        if human_target_alias:
            approval_parts.append(f"--target-alias {human_target_alias}")
        approval_parts.extend(
            [
                "--human-note",
                _quote_stdio_text("Live soak session completed without thread drift."),
            ]
        )
        plan.append(("Escalate to the human governor", " ".join(approval_parts)))

    plan.append(
        ("Write end evidence snapshot", f"mepdmsnapshot --context {context_id} --label end --limit {snapshot_limit}")
    )
    return plan


async def main() -> None:
    if not KEY_PATH:
        raise SystemExit("MEP_BOT_KEY_PATH is required")
    if not TARGET_NODE:
        raise SystemExit("MEP_TARGET_NODE is required")

    client = MEPClient(KEY_PATH)
    await client.register()
    session_safety = MEPClient.build_session_safety_metadata(
        max_turns=MAX_TURNS,
        max_duration_seconds=MAX_DURATION_SECONDS,
        checkpoint_interval=CHECKPOINT_INTERVAL,
    )

    submit = await client.submit_dm(
        REVIEW_REQUEST,
        TARGET_NODE,
        target_alias=TARGET_ALIAS,
        intent_type="review.request",
        context_id=CONTEXT_ID,
        reply_to_task_id=REPLY_TO_TASK_ID,
        reply_to_message_id=REPLY_TO_MESSAGE_ID,
        turn_type="review_request",
        human_note="Guarded threaded review starter from scripts/threaded_review_example.py",
        session_safety=session_safety,
        turn_index=1,
    )
    live_context_id = submit.get("context_id") or CONTEXT_ID
    print(
        "review_request",
        {
            "task_id": submit.get("json", {}).get("task_id"),
            "context_id": live_context_id,
            "message_id": submit.get("message_id"),
            "session_safety": session_safety,
            "turn_index": 1,
        },
    )
    print("next_stdio_commands")
    for label, command in build_stdio_soak_plan(
        live_context_id,
        snapshot_limit=SNAPSHOT_LIMIT,
        human_target_node=HUMAN_TARGET_NODE,
        human_target_alias=HUMAN_TARGET_ALIAS,
    ):
        print(f"- {label}: {command}")


if __name__ == "__main__":
    asyncio.run(main())
