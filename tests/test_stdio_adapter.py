import json
import unittest
from unittest.mock import AsyncMock, patch

from clients.shared.stdio_adapter import StdioAdapter


class TestStdioAdapter(unittest.IsolatedAsyncioTestCase):
    def _make_adapter(self):
        patcher = patch("clients.shared.stdio_adapter.MEPClient")
        client_cls = patcher.start()
        self.addCleanup(patcher.stop)
        client = client_cls.return_value
        client.node_id = "node_adapter"
        adapter = StdioAdapter("codex", "codex-agent", "unused.pem")
        return adapter, client

    async def test_handle_result_stores_structured_interbot_payload(self):
        adapter, client = self._make_adapter()
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "message_id": "message_review_request",
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            }
        )
        client.parse_interbot_payload.return_value = json.loads(payload)

        with patch("builtins.print") as print_mock:
            await adapter._handle_result({"task_id": "task_review_request", "result_payload": payload})

        self.assertIn("task_review_request", adapter._recent_interbot_results)
        self.assertEqual(
            adapter._recent_interbot_results["task_review_request"]["message"]["conversation"]["context_id"],
            "pr154-review",
        )
        print_mock.assert_any_call(
            "[codex] stored structured dm result task_review_request context=pr154-review"
        )

    async def test_dispatch_line_safe_reply_uses_stored_inbound_message(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_safe_dm_reply = AsyncMock(
            return_value={
                "reply_action": "reply",
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_safe_reply"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_review_request 3 "I approve with conditions." '
                '--checkpoint-summary "Checkpoint: 3 turns reached." '
                '--turn-type review_response --intent review.response --priority high'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once_with(
            "I approve with conditions.",
            adapter._recent_interbot_results["task_review_request"]["message"],
            next_turn_index=3,
            checkpoint_summary="Checkpoint: 3 turns reached.",
            inbound_task_id="task_review_request",
            turn_type="review_response",
            intent_type="review.response",
            priority="high",
        )
        print_mock.assert_any_call("[codex] safe reply reply task task_safe_reply context=pr154-review")

    async def test_dispatch_line_dmlist_reports_when_empty(self):
        adapter, _client = self._make_adapter()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist")

        self.assertTrue(keep_going)
        print_mock.assert_any_call("[codex] no stored structured dm results")

    async def test_dispatch_line_dmlist_shows_recent_structured_dm_results(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "intent": {"type": "coordination.request"},
                "task": {"instructions": "Checkpoint summary"},
            },
        }

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist")

        self.assertTrue(keep_going)
        print_mock.assert_any_call("[codex] recent structured dm results:")
        print_mock.assert_any_call(
            "[codex] - task_id=task_checkpoint context_id=pr154-review "
            "message_id=message_checkpoint source=node_reviewer "
            "turn_type=checkpoint intent=coordination.request"
        )
        print_mock.assert_any_call(
            "[codex] - task_id=task_review_request context_id=pr154-review "
            "message_id=message_review_request source=node_reviewer "
            "turn_type=review_request intent=review.request"
        )

    async def test_dispatch_line_safe_reply_reports_stop_without_submitting_dm(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_safe_dm_reply = AsyncMock(
            return_value={
                "reply_action": "stop",
                "safety": {"violations": ["max_turns_exceeded"]},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_review_request 5 "I approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once()
        print_mock.assert_any_call("[codex] safe reply stopped for task_review_request: max_turns_exceeded")

    async def test_dispatch_line_safe_reply_reports_validation_errors(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_safe_dm_reply = AsyncMock(side_effect=ValueError("next_turn_index must be at least 1"))

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_review_request 0 "I approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once()
        print_mock.assert_any_call("[codex] safe dm reply error: next_turn_index must be at least 1")


if __name__ == "__main__":
    unittest.main()
