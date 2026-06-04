import json
import os
import tempfile
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

    async def test_dispatch_line_structured_dm_accepts_session_safety_flags(self):
        adapter, client = self._make_adapter()
        client.build_session_safety_metadata.return_value = {
            "max_turns": 12,
            "max_duration_seconds": 3600,
            "checkpoint_interval": 3,
            "started_at_ms": 1_777_000_000_000,
        }
        client.submit_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_threaded_dm"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmx node_reviewer "Please review PR 154." '
                "--context pr154-review --turn-type review_request --intent review.request "
                "--priority high --max-turns 12 --max-duration-seconds 3600 "
                "--checkpoint-interval 3"
            )

        self.assertTrue(keep_going)
        client.build_session_safety_metadata.assert_called_once_with(
            max_turns=12,
            max_duration_seconds=3600,
            checkpoint_interval=3,
        )
        client.submit_dm.assert_awaited_once_with(
            "Please review PR 154.",
            "node_reviewer",
            context_id="pr154-review",
            reply_to_task_id=None,
            reply_to_message_id=None,
            turn_type="review_request",
            intent_type="review.request",
            priority="high",
            session_safety={
                "max_turns": 12,
                "max_duration_seconds": 3600,
                "checkpoint_interval": 3,
                "started_at_ms": 1_777_000_000_000,
            },
            turn_index=1,
        )
        print_mock.assert_any_call("[codex] sent threaded dm task task_threaded_dm to node_reviewer context=pr154-review")

    async def test_dispatch_line_structured_dm_rejects_unknown_option(self):
        adapter, client = self._make_adapter()
        client.submit_dm = AsyncMock()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmx node_reviewer "Please review PR 154." --bogus nope'
            )

        self.assertTrue(keep_going)
        client.submit_dm.assert_not_awaited()
        print_mock.assert_any_call("[codex] unknown option --bogus")

    async def test_dispatch_line_structured_dm_reports_invalid_session_safety(self):
        adapter, client = self._make_adapter()
        client.build_session_safety_metadata.side_effect = ValueError(
            "checkpoint_interval must be a positive integer"
        )
        client.submit_dm = AsyncMock()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmx node_reviewer "Please review PR 154." --checkpoint-interval 0'
            )

        self.assertTrue(keep_going)
        client.submit_dm.assert_not_awaited()
        print_mock.assert_any_call(
            "[codex] threaded dm error: checkpoint_interval must be a positive integer"
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
            human_note=None,
        )
        print_mock.assert_any_call("[codex] safe reply task task_safe_reply context=pr154-review")

    async def test_dispatch_line_safe_reply_accepts_human_note(self):
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
                '--turn-type review_response --intent review.response '
                '--human-note "Human asked to preserve final release context."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once_with(
            "I approve with conditions.",
            adapter._recent_interbot_results["task_review_request"]["message"],
            next_turn_index=3,
            checkpoint_summary=None,
            inbound_task_id="task_review_request",
            turn_type="review_response",
            intent_type="review.response",
            priority=None,
            human_note="Human asked to preserve final release context.",
        )
        print_mock.assert_any_call("[codex] safe reply task task_safe_reply context=pr154-review")

    async def test_dispatch_line_safe_reply_reports_checkpoint_action(self):
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
                "reply_action": "checkpoint",
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_checkpoint_reply"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_review_request 3 "Checkpoint summary." '
                '--checkpoint-summary "Checkpoint: 3 turns reached."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once()
        print_mock.assert_any_call(
            "[codex] safe checkpoint task task_checkpoint_reply context=pr154-review"
        )

    async def test_dispatch_line_safe_reply_rejects_unknown_option(self):
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
        client.submit_safe_dm_reply = AsyncMock()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_review_request 3 "I approve with conditions." --bogus nope'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_not_awaited()
        print_mock.assert_any_call("[codex] unknown option --bogus")

    async def test_handle_event_auto_accepts_incoming_live_call_when_enabled(self):
        adapter, client = self._make_adapter()
        adapter.live_call_enabled = True
        adapter.call_auto_accept = True
        client.send_ws_event = AsyncMock(return_value=True)

        with patch("builtins.print") as print_mock:
            await adapter._handle_event({"event": "call.incoming", "context_id": "ctx-live", "caller": "node_peer"})

        client.send_ws_event.assert_awaited_once_with({"event": "call.accept", "context_id": "ctx-live"})
        print_mock.assert_any_call("[codex] auto-accepted live call context=ctx-live caller=node_peer")

    async def test_handle_event_replies_to_call_ping(self):
        adapter, client = self._make_adapter()
        client.send_ws_event = AsyncMock(return_value=True)

        await adapter._handle_event({"event": "call.ping", "context_id": "ctx-ping"})

        client.send_ws_event.assert_awaited_once_with({"event": "call.pong", "context_id": "ctx-ping"})

    async def test_dispatch_line_mepcall_sends_invite(self):
        adapter, client = self._make_adapter()
        client.send_ws_event = AsyncMock(return_value=True)

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                "mepcall node_peer --context ctx-live --timeout-ms 12000 --grace-ms 4000"
            )

        self.assertTrue(keep_going)
        client.send_ws_event.assert_awaited_once_with(
            {
                "event": "call.invite",
                "context_id": "ctx-live",
                "callee": "node_peer",
                "timeout_ms": 12000,
                "reconnect_grace_ms": 4000,
            }
        )
        print_mock.assert_any_call("[codex] live call invite sent context=ctx-live callee=node_peer")

    async def test_dispatch_line_mepcallframe_auto_increments_seq(self):
        adapter, client = self._make_adapter()
        client.send_ws_event = AsyncMock(return_value=True)

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line('mepcallframe ctx-live "hello over live lane"')

        self.assertTrue(keep_going)
        client.send_ws_event.assert_awaited_once_with(
            {
                "event": "call.frame",
                "context_id": "ctx-live",
                "seq": 0,
                "content_type": "text/plain",
                "payload": "hello over live lane",
            }
        )
        self.assertEqual(adapter._call_seq_by_context["ctx-live"], 1)
        print_mock.assert_any_call("[codex] live frame sent context=ctx-live seq=0")

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

    async def test_dispatch_line_dmlist_filters_by_context(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }
        adapter._recent_interbot_results["task_other_context"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_other",
                "source": {"node_id": "node_other"},
                "conversation": {"context_id": "incident-42", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist --context pr154-review")

        self.assertTrue(keep_going)
        print_mock.assert_any_call("[codex] recent structured dm results for context=pr154-review:")
        print_mock.assert_any_call(
            "[codex] - task_id=task_review_request context_id=pr154-review "
            "message_id=message_review_request source=node_reviewer "
            "turn_type=review_request intent=review.request"
        )
        self.assertNotIn(
            unittest.mock.call(
                "[codex] - task_id=task_other_context context_id=incident-42 "
                "message_id=message_other source=node_other "
                "turn_type=review_request intent=review.request"
            ),
            print_mock.mock_calls,
        )

    async def test_dispatch_line_dmlist_honors_limit(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "intent": {"type": "coordination.request"},
            },
        }

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist --limit 1")

        self.assertTrue(keep_going)
        print_mock.assert_any_call("[codex] recent structured dm results:")
        print_mock.assert_any_call(
            "[codex] - task_id=task_checkpoint context_id=pr154-review "
            "message_id=message_checkpoint source=node_reviewer "
            "turn_type=checkpoint intent=coordination.request"
        )
        self.assertNotIn(
            unittest.mock.call(
                "[codex] - task_id=task_review_request context_id=pr154-review "
                "message_id=message_review_request source=node_reviewer "
                "turn_type=review_request intent=review.request"
            ),
            print_mock.mock_calls,
        )

    async def test_dispatch_line_dmlist_json_outputs_filtered_snapshot(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "intent": {"type": "coordination.request"},
                "task": {"instructions": "Checkpoint summary"},
            },
        }
        adapter._recent_interbot_results["task_other_context"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_other",
                "source": {"node_id": "node_other"},
                "conversation": {"context_id": "incident-42", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist --context pr154-review --limit 1 --json")

        self.assertTrue(keep_going)
        self.assertEqual(print_mock.call_count, 1)
        snapshot = json.loads(print_mock.call_args.args[0])
        self.assertEqual(snapshot["platform"], "codex")
        self.assertEqual(snapshot["context_filter"], "pr154-review")
        self.assertEqual(snapshot["limit"], 1)
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(
            snapshot["results"],
            [
                {
                    "task_id": "task_checkpoint",
                    "context_id": "pr154-review",
                    "message_id": "message_checkpoint",
                    "source_node_id": "node_reviewer",
                    "turn_type": "checkpoint",
                    "intent_type": "coordination.request",
                    "payload_text": '{"spec_version":"mep.interbot.v1"}',
                    "message": {
                        "message_id": "message_checkpoint",
                        "source": {"node_id": "node_reviewer"},
                        "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                        "intent": {"type": "coordination.request"},
                        "task": {"instructions": "Checkpoint summary"},
                    },
                }
            ],
        )

    async def test_dispatch_line_dmlist_json_outputs_empty_snapshot(self):
        adapter, _client = self._make_adapter()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist --json")

        self.assertTrue(keep_going)
        self.assertEqual(print_mock.call_count, 1)
        snapshot = json.loads(print_mock.call_args.args[0])
        self.assertEqual(
            snapshot,
            {
                "platform": "codex",
                "context_filter": None,
                "limit": None,
                "count": 0,
                "results": [],
            },
        )

    async def test_dispatch_line_dmlist_rejects_unknown_option(self):
        adapter, _client = self._make_adapter()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmlist --bogus nope")

        self.assertTrue(keep_going)
        print_mock.assert_any_call("[codex] unknown option --bogus")

    async def test_dispatch_line_dmsnapshot_writes_filtered_snapshot_to_default_file(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        adapter._recent_interbot_results["task_other_context"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_other",
                "source": {"node_id": "node_other"},
                "conversation": {"context_id": "incident-42", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                with patch("builtins.print") as print_mock:
                    keep_going = await adapter._dispatch_line(
                        "mepdmsnapshot --context pr154-review --label start --limit 1"
                    )
            finally:
                os.chdir(current_dir)

            self.assertTrue(keep_going)
            snapshot_path = os.path.join(temp_dir, "soak-pr154-review-start.json")
            self.assertTrue(os.path.exists(snapshot_path))
            with open(snapshot_path, encoding="utf-8") as snapshot_file:
                snapshot = json.load(snapshot_file)

        self.assertEqual(snapshot["platform"], "codex")
        self.assertEqual(snapshot["context_filter"], "pr154-review")
        self.assertEqual(snapshot["limit"], 1)
        self.assertEqual(snapshot["snapshot_label"], "start")
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["results"][0]["task_id"], "task_review_request")
        self.assertIn("captured_at_utc", snapshot)
        print_mock.assert_any_call(
            f"[codex] wrote structured dm snapshot {snapshot_path} label=start count=1 context=pr154-review"
        )

    async def test_dispatch_line_dmsnapshot_honors_explicit_output_path(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                output_path = os.path.join(temp_dir, "evidence", "mid.json")
                with patch("builtins.print") as print_mock:
                    keep_going = await adapter._dispatch_line(
                        f'mepdmsnapshot --label mid --out "{output_path}"'
                    )
            finally:
                os.chdir(current_dir)

            self.assertTrue(keep_going)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as snapshot_file:
                snapshot = json.load(snapshot_file)

        self.assertEqual(snapshot["snapshot_label"], "mid")
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["context_filter"], None)
        print_mock.assert_any_call(
            f"[codex] wrote structured dm snapshot {output_path} label=mid count=1"
        )

    async def test_dispatch_line_dmsnapshot_writes_empty_snapshot(self):
        adapter, _client = self._make_adapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                output_path = os.path.join(temp_dir, "empty.json")
                with patch("builtins.print") as print_mock:
                    keep_going = await adapter._dispatch_line(
                        f'mepdmsnapshot --label end --out "{output_path}"'
                    )
            finally:
                os.chdir(current_dir)

            self.assertTrue(keep_going)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as snapshot_file:
                snapshot = json.load(snapshot_file)

        self.assertEqual(snapshot["snapshot_label"], "end")
        self.assertEqual(snapshot["count"], 0)
        self.assertEqual(snapshot["results"], [])
        print_mock.assert_any_call(
            f"[codex] wrote structured dm snapshot {output_path} label=end count=0"
        )

    async def test_dispatch_line_dmsnapshot_requires_label(self):
        adapter, _client = self._make_adapter()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmsnapshot --context pr154-review")

        self.assertTrue(keep_going)
        print_mock.assert_any_call(
            "[codex] usage: mepdmsnapshot --label <label> [--context <context_id>] [--limit <count>] [--out <file>]"
        )

    async def test_dispatch_line_dmsnapshot_rejects_unknown_option(self):
        adapter, _client = self._make_adapter()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line("mepdmsnapshot --label start --json")

        self.assertTrue(keep_going)
        print_mock.assert_any_call("[codex] unknown option --json")

    async def test_dispatch_line_dmsnapshot_sanitizes_default_filename_components(self):
        adapter, _client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": '{"spec_version":"mep.interbot.v1"}',
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr/154..review", "turn_type": "review_request"},
                "intent": {"type": "review.request"},
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                with patch("builtins.print") as print_mock:
                    keep_going = await adapter._dispatch_line(
                        "mepdmsnapshot --context pr/154..review --label start?"
                    )
            finally:
                os.chdir(current_dir)

            self.assertTrue(keep_going)
            snapshot_path = os.path.join(temp_dir, "soak-pr-154--review-start.json")
            self.assertTrue(os.path.exists(snapshot_path))

        print_mock.assert_any_call(
            "[codex] wrote structured dm snapshot "
            + os.path.join(temp_dir, "soak-pr-154--review-start.json")
            + " label=start? count=1 context=pr/154..review"
        )

    async def test_dispatch_line_dmsnapshot_rejects_output_path_outside_cwd(self):
        adapter, _client = self._make_adapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = os.getcwd()
            os.chdir(temp_dir)
            try:
                outside_path = os.path.abspath(os.path.join(temp_dir, "..", "escape.json"))
                with patch("builtins.print") as print_mock:
                    keep_going = await adapter._dispatch_line(
                        f'mepdmsnapshot --label start --out "{outside_path}"'
                    )
            finally:
                os.chdir(current_dir)

        self.assertTrue(keep_going)
        self.assertFalse(os.path.exists(outside_path))
        print_mock.assert_any_call("[codex] --out must stay within the current working directory")

    async def test_dispatch_line_review_verdict_uses_stored_inbound_message(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_review_verdict_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_review_verdict"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmverdict task_review_request approve_with_conditions '
                '"Threading model is sound." '
                '--condition "Document reply expectations." '
                '--condition "Keep reply_mode=new_dm." '
                '--recommendation "Merge after the docs note lands." '
                '--priority high'
            )

        self.assertTrue(keep_going)
        client.submit_review_verdict_dm.assert_awaited_once_with(
            "approve_with_conditions",
            "Threading model is sound.",
            "node_reviewer",
            context_id="pr154-review",
            target_alias="Reviewer",
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            conditions=["Document reply expectations.", "Keep reply_mode=new_dm."],
            human_recommendation="Merge after the docs note lands.",
            priority="high",
            human_note=None,
        )
        print_mock.assert_any_call("[codex] review verdict sent task task_review_verdict context=pr154-review")

    async def test_dispatch_line_review_verdict_accepts_human_note(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_review_verdict_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_review_verdict"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmverdict task_review_request approve_with_conditions '
                '"Threading model is sound." '
                '--condition "Document reply expectations." '
                '--recommendation "Merge after the docs note lands." '
                '--human-note "Human requested one extra release-timing check."'
            )

        self.assertTrue(keep_going)
        client.submit_review_verdict_dm.assert_awaited_once_with(
            "approve_with_conditions",
            "Threading model is sound.",
            "node_reviewer",
            context_id="pr154-review",
            target_alias="Reviewer",
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            conditions=["Document reply expectations."],
            human_recommendation="Merge after the docs note lands.",
            priority="normal",
            human_note="Human requested one extra release-timing check.",
        )
        print_mock.assert_any_call("[codex] review verdict sent task task_review_verdict context=pr154-review")

    async def test_dispatch_line_review_verdict_accepts_unquoted_multiword_rationale(self):
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
        client.submit_review_verdict_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_review_verdict"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                "mepdmverdict task_review_request approve_with_conditions "
                "Threading model is sound. --condition \"Document reply expectations.\""
            )

        self.assertTrue(keep_going)
        client.submit_review_verdict_dm.assert_awaited_once_with(
            "approve_with_conditions",
            "Threading model is sound.",
            "node_reviewer",
            context_id="pr154-review",
            target_alias=None,
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            conditions=["Document reply expectations."],
            human_recommendation=None,
            priority="normal",
            human_note=None,
        )
        print_mock.assert_any_call("[codex] review verdict sent task task_review_verdict context=pr154-review")

    async def test_dispatch_line_review_verdict_accepts_context_selector(self):
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
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "task": {"instructions": "Checkpoint summary."},
            },
        }
        client.submit_review_verdict_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_review_verdict"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmverdict --context pr154-review approve_with_conditions "Threading model is sound."'
            )

        self.assertTrue(keep_going)
        client.submit_review_verdict_dm.assert_awaited_once_with(
            "approve_with_conditions",
            "Threading model is sound.",
            "node_reviewer",
            context_id="pr154-review",
            target_alias=None,
            reply_to_task_id="task_checkpoint",
            reply_to_message_id="message_checkpoint",
            conditions=None,
            human_recommendation=None,
            priority="normal",
            human_note=None,
        )
        print_mock.assert_any_call("[codex] review verdict sent task task_review_verdict context=pr154-review")

    async def test_dispatch_line_review_verdict_propagates_turn_index_when_available(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer"},
                "conversation": {
                    "context_id": "pr154-review",
                    "turn_type": "review_request",
                    "turn_index": 1,
                },
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_review_verdict_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_review_verdict"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmverdict task_review_request approve_with_conditions "Threading model is sound."'
            )

        self.assertTrue(keep_going)
        client.submit_review_verdict_dm.assert_awaited_once_with(
            "approve_with_conditions",
            "Threading model is sound.",
            "node_reviewer",
            context_id="pr154-review",
            target_alias=None,
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            conditions=None,
            human_recommendation=None,
            priority="normal",
            human_note=None,
            turn_index=2,
        )
        print_mock.assert_any_call("[codex] review verdict sent task task_review_verdict context=pr154-review")

    async def test_dispatch_line_review_verdict_reports_validation_errors(self):
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
        client.submit_review_verdict_dm = AsyncMock(side_effect=ValueError("unsupported review verdict: maybe"))

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmverdict task_review_request maybe "Threading model is sound."'
            )

        self.assertTrue(keep_going)
        client.submit_review_verdict_dm.assert_awaited_once()
        print_mock.assert_any_call("[codex] review verdict error: unsupported review verdict: maybe")

    async def test_dispatch_line_human_approval_request_uses_stored_inbound_message(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_verdict"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_verdict",
                "source": {"node_id": "node_governor", "alias": "Governor"},
                "conversation": {"context_id": "pr154-review", "turn_type": "approval"},
                "task": {"instructions": "Review verdict ready."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_human_approval"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval task_review_verdict '
                '"Two bots approve with conditions and no code blocker remains." '
                '--review-decision approve_with_conditions '
                '--blocker "Need explicit merge confirmation from the human governor." '
                '--next-action "Merge after final human approval." '
                '--priority high'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once_with(
            "Two bots approve with conditions and no code blocker remains.",
            "node_governor",
            context_id="pr154-review",
            decision_type="merge_decision",
            target_alias="Governor",
            reply_to_task_id="task_review_verdict",
            reply_to_message_id="message_review_verdict",
            review_decision="approve_with_conditions",
            blockers=["Need explicit merge confirmation from the human governor."],
            recommended_next_action="Merge after final human approval.",
            priority="high",
            human_note=None,
        )
        print_mock.assert_any_call(
            "[codex] human approval request sent task task_human_approval context=pr154-review"
        )

    async def test_dispatch_line_human_approval_request_accepts_context_selector(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        adapter._recent_interbot_results["task_review_verdict"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_verdict",
                "source": {"node_id": "node_governor", "alias": "Governor"},
                "conversation": {"context_id": "pr154-review", "turn_type": "approval"},
                "task": {"instructions": "Review verdict ready."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_human_approval"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval --context pr154-review "Two bots approve with conditions." '
                '--review-decision approve_with_conditions'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once_with(
            "Two bots approve with conditions.",
            "node_governor",
            context_id="pr154-review",
            decision_type="merge_decision",
            target_alias="Governor",
            reply_to_task_id="task_review_verdict",
            reply_to_message_id="message_review_verdict",
            review_decision="approve_with_conditions",
            blockers=None,
            recommended_next_action=None,
            priority="high",
            human_note=None,
        )
        print_mock.assert_any_call(
            "[codex] human approval request sent task task_human_approval context=pr154-review"
        )

    async def test_dispatch_line_human_approval_request_propagates_turn_index_when_available(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_verdict"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_verdict",
                "source": {"node_id": "node_governor", "alias": "Governor"},
                "conversation": {
                    "context_id": "pr154-review",
                    "turn_type": "approval",
                    "turn_index": 2,
                },
                "task": {"instructions": "Review verdict ready."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_human_approval"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval task_review_verdict "Two bots approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once_with(
            "Two bots approve with conditions.",
            "node_governor",
            context_id="pr154-review",
            decision_type="merge_decision",
            target_alias="Governor",
            reply_to_task_id="task_review_verdict",
            reply_to_message_id="message_review_verdict",
            review_decision=None,
            blockers=None,
            recommended_next_action=None,
            priority="high",
            human_note=None,
            turn_index=3,
        )
        print_mock.assert_any_call(
            "[codex] human approval request sent task task_human_approval context=pr154-review"
        )

    async def test_dispatch_line_human_approval_request_accepts_target_override(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_human_approval"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval task_review_request '
                '"Two bots approve with conditions and no code blocker remains." '
                '--review-decision approve_with_conditions '
                '--blocker "Need explicit merge confirmation from the human governor." '
                '--next-action "Merge after final human approval." '
                '--target-node node_governor --target-alias Governor'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once_with(
            "Two bots approve with conditions and no code blocker remains.",
            "node_governor",
            context_id="pr154-review",
            decision_type="merge_decision",
            target_alias="Governor",
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            review_decision="approve_with_conditions",
            blockers=["Need explicit merge confirmation from the human governor."],
            recommended_next_action="Merge after final human approval.",
            priority="high",
            human_note=None,
        )
        print_mock.assert_any_call(
            "[codex] human approval request sent task task_human_approval context=pr154-review"
        )

    async def test_dispatch_line_human_approval_request_accepts_human_note(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_human_approval"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval task_review_request '
                '"Two bots approve with conditions and no code blocker remains." '
                '--review-decision approve_with_conditions '
                '--blocker "Need explicit merge confirmation from the human governor." '
                '--next-action "Merge after final human approval." '
                '--target-node node_governor --target-alias Governor '
                '--human-note "Human asked for a final release-window check."'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once_with(
            "Two bots approve with conditions and no code blocker remains.",
            "node_governor",
            context_id="pr154-review",
            decision_type="merge_decision",
            target_alias="Governor",
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            review_decision="approve_with_conditions",
            blockers=["Need explicit merge confirmation from the human governor."],
            recommended_next_action="Merge after final human approval.",
            priority="high",
            human_note="Human asked for a final release-window check.",
        )
        print_mock.assert_any_call(
            "[codex] human approval request sent task task_human_approval context=pr154-review"
        )

    async def test_dispatch_line_human_approval_request_does_not_inherit_alias_when_target_only_is_overridden(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_request"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_request",
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {"instructions": "Please review this PR."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            return_value={
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_human_approval"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval task_review_request '
                '"Two bots approve with conditions and no code blocker remains." '
                '--review-decision approve_with_conditions '
                '--blocker "Need explicit merge confirmation from the human governor." '
                '--next-action "Merge after final human approval." '
                '--target-node node_governor'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once_with(
            "Two bots approve with conditions and no code blocker remains.",
            "node_governor",
            context_id="pr154-review",
            decision_type="merge_decision",
            target_alias=None,
            reply_to_task_id="task_review_request",
            reply_to_message_id="message_review_request",
            review_decision="approve_with_conditions",
            blockers=["Need explicit merge confirmation from the human governor."],
            recommended_next_action="Merge after final human approval.",
            priority="high",
            human_note=None,
        )
        print_mock.assert_any_call(
            "[codex] human approval request sent task task_human_approval context=pr154-review"
        )

    async def test_dispatch_line_human_approval_request_reports_validation_errors(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_review_verdict"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_review_verdict",
                "source": {"node_id": "node_governor"},
                "conversation": {"context_id": "pr154-review", "turn_type": "approval"},
                "task": {"instructions": "Review verdict ready."},
            },
        }
        client.submit_human_approval_request_dm = AsyncMock(
            side_effect=ValueError("unsupported review decision: maybe")
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmhumanapproval task_review_verdict '
                '"Two bots approve with conditions." --review-decision maybe'
            )

        self.assertTrue(keep_going)
        client.submit_human_approval_request_dm.assert_awaited_once()
        print_mock.assert_any_call(
            "[codex] human approval request error: unsupported review decision: maybe"
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

    async def test_dispatch_line_safe_reply_accepts_context_selector(self):
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
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "task": {"instructions": "Checkpoint summary."},
            },
        }
        client.submit_safe_dm_reply = AsyncMock(
            return_value={
                "reply_action": "reply",
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_followup"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe --context pr154-review 3 "I approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once_with(
            "I approve with conditions.",
            {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "task": {"instructions": "Checkpoint summary."},
            },
            next_turn_index=3,
            checkpoint_summary=None,
            inbound_task_id="task_checkpoint",
            turn_type=None,
            intent_type=None,
            priority=None,
            human_note=None,
        )
        print_mock.assert_any_call("[codex] safe reply task task_followup context=pr154-review")

    async def test_dispatch_line_safe_reply_accepts_auto_turn_index(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {
                    "context_id": "pr154-review",
                    "turn_type": "checkpoint",
                    "turn_index": 3,
                },
                "task": {"instructions": "Checkpoint summary."},
            },
        }
        client.submit_safe_dm_reply = AsyncMock(
            return_value={
                "reply_action": "reply",
                "status_code": 200,
                "json": {"status": "success", "task_id": "task_followup"},
                "context_id": "pr154-review",
            }
        )

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_checkpoint auto "I approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_awaited_once_with(
            "I approve with conditions.",
            adapter._recent_interbot_results["task_checkpoint"]["message"],
            next_turn_index=4,
            checkpoint_summary=None,
            inbound_task_id="task_checkpoint",
            turn_type=None,
            intent_type=None,
            priority=None,
            human_note=None,
        )
        print_mock.assert_any_call("[codex] safe reply task task_followup context=pr154-review")

    async def test_dispatch_line_safe_reply_auto_turn_requires_turn_index(self):
        adapter, client = self._make_adapter()
        adapter._recent_interbot_results["task_checkpoint"] = {
            "payload_text": "{}",
            "message": {
                "message_id": "message_checkpoint",
                "source": {"node_id": "node_reviewer"},
                "conversation": {"context_id": "pr154-review", "turn_type": "checkpoint"},
                "task": {"instructions": "Checkpoint summary."},
            },
        }
        client.submit_safe_dm_reply = AsyncMock()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe task_checkpoint auto "I approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_not_awaited()
        print_mock.assert_any_call(
            "[codex] stored structured dm result task_checkpoint is missing conversation.turn_index; "
            "pass an explicit next_turn_index"
        )

    async def test_dispatch_line_safe_reply_reports_missing_context_selector(self):
        adapter, client = self._make_adapter()
        client.submit_safe_dm_reply = AsyncMock()

        with patch("builtins.print") as print_mock:
            keep_going = await adapter._dispatch_line(
                'mepdmreplysafe --context missing-thread 3 "I approve with conditions."'
            )

        self.assertTrue(keep_going)
        client.submit_safe_dm_reply.assert_not_awaited()
        print_mock.assert_any_call("[codex] no stored structured dm results for context=missing-thread")

    async def test_dispatch_line_safe_reply_reports_validation_errors(self):
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
