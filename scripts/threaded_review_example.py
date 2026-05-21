import asyncio
import os

from clients.shared.mep_client import MEPClient


TARGET_NODE = os.getenv("MEP_TARGET_NODE", "").strip()
TARGET_ALIAS = os.getenv("MEP_TARGET_ALIAS", "").strip() or None
HUMAN_TARGET_NODE = os.getenv("MEP_HUMAN_TARGET_NODE", "").strip() or None
HUMAN_TARGET_ALIAS = os.getenv("MEP_HUMAN_TARGET_ALIAS", "").strip() or None
KEY_PATH = os.getenv("MEP_BOT_KEY_PATH", "").strip()
CONTEXT_ID = os.getenv("MEP_CONTEXT_ID", "example-threaded-review-001").strip()
REPLY_TO_TASK_ID = os.getenv("MEP_REPLY_TO_TASK_ID", "").strip() or None
REPLY_TO_MESSAGE_ID = os.getenv("MEP_REPLY_TO_MESSAGE_ID", "").strip() or None


async def main() -> None:
    if not KEY_PATH:
        raise SystemExit("MEP_BOT_KEY_PATH is required")
    if not TARGET_NODE:
        raise SystemExit("MEP_TARGET_NODE is required")

    client = MEPClient(KEY_PATH)
    await client.register()

    review_request = (
        "Please review the current PR and reply with one of: "
        "approve, approve_with_conditions, request_changes, or block. "
        "Also include one short rationale."
    )
    submit = await client.submit_dm(
        review_request,
        TARGET_NODE,
        target_alias=TARGET_ALIAS,
        intent_type="review.request",
        context_id=CONTEXT_ID,
        reply_to_task_id=REPLY_TO_TASK_ID,
        reply_to_message_id=REPLY_TO_MESSAGE_ID,
        turn_type="review_request",
        human_note="Example structured review flow from scripts/threaded_review_example.py",
    )
    print(
        "review_request",
        {
            "task_id": submit.get("json", {}).get("task_id"),
            "context_id": submit.get("context_id"),
            "message_id": submit.get("message_id"),
        },
    )

    checkpoint = (
        "Checkpoint: review request sent. Waiting for structured verdict. "
        "If you respond with conditions, include the top two blocking concerns first."
    )
    checkpoint_submit = await client.submit_checkpoint_dm(
        checkpoint,
        TARGET_NODE,
        target_alias=TARGET_ALIAS,
        context_id=submit.get("context_id") or CONTEXT_ID,
        reply_to_task_id=submit.get("json", {}).get("task_id"),
        reply_to_message_id=submit.get("message_id"),
        human_note="Example checkpoint turn in the same threaded review session",
    )
    print(
        "checkpoint",
        {
            "task_id": checkpoint_submit.get("json", {}).get("task_id"),
            "context_id": checkpoint_submit.get("context_id"),
            "message_id": checkpoint_submit.get("message_id"),
        },
    )

    if HUMAN_TARGET_NODE:
        approval_request = await client.submit_human_approval_request_dm(
            "Bots completed the review pass. A human merge decision is needed.",
            HUMAN_TARGET_NODE,
            target_alias=HUMAN_TARGET_ALIAS,
            context_id=submit.get("context_id") or CONTEXT_ID,
            reply_to_task_id=checkpoint_submit.get("json", {}).get("task_id"),
            reply_to_message_id=checkpoint_submit.get("message_id"),
            review_decision="approve_with_conditions",
            blockers=["Confirm final merge timing"],
            recommended_next_action="Merge after human approval.",
            human_note="Optional human approval handoff from scripts/threaded_review_example.py",
        )
        print(
            "human_approval_request",
            {
                "task_id": approval_request.get("json", {}).get("task_id"),
                "context_id": approval_request.get("context_id"),
                "message_id": approval_request.get("message_id"),
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
