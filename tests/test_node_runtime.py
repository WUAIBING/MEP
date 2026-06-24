import argparse
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from node.identity import MEPIdentity
from node import mep_runtime


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


class _FakeIdentity:
    node_id = "node_runtime"
    pub_pem = "pub"
    x25519_public_key = "encpub"

    def get_auth_headers(self, payload: str) -> dict:
        return {"X-MEP-NodeID": self.node_id, "X-MEP-Signature": "sig"}

    def sign(self, node_id: str, timestamp: str) -> str:
        return "sig"


def _runtime_node() -> mep_runtime.RuntimeNode:
    return mep_runtime.RuntimeNode(
        identity=_FakeIdentity(),
        hub_url="http://hub",
        ws_url="ws://hub",
        adapter=mep_runtime.MockAdapter(),
    )


class _FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.pings = 0
        self.sent = []

    async def recv(self):
        item = self.messages.pop(0)
        if item == "timeout":
            raise asyncio.TimeoutError
        return item

    async def ping(self):
        self.pings += 1

    async def send(self, payload):
        self.sent.append(payload)


class _FakeConnectContext:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRuntime:
    async def run_forever(self):
        return 0


class _FakeCompletedProcess:
    def __init__(self, stdout=""):
        self.stdout = stdout


class TestMockAdapter(unittest.TestCase):
    def test_mock_adapter_labels_compute_chat_and_data_markets(self):
        adapter = mep_runtime.MockAdapter()

        compute = adapter.generate_reply("compute payload", {"id": "task_compute", "bounty": 1.0})
        chat = adapter.generate_reply("hello", {"id": "task_chat", "bounty": 0.0})
        data = adapter.generate_reply("dataset", {"id": "task_data", "bounty": -0.25})

        self.assertIn("market=compute", compute)
        self.assertIn("market=chat", chat)
        self.assertIn("DM received", chat)
        self.assertIn("market=data", data)
        self.assertIn("Data purchase acknowledged", data)


class TestRuntimeUx(unittest.TestCase):
    def test_status_badges_do_not_mark_heartbeat_or_ai_ready_for_mock_offline_node(self):
        badges = mep_runtime._status_badges(  # noqa: SLF001 - direct unit test of helper
            {"registered": True, "ws_connected": False, "last_heartbeat": 100.0, "availability": "offline"},
            ai_ready=False,
        )
        self.assertTrue(badges["REGISTERED"])
        self.assertFalse(badges["HEARTBEATING"])
        self.assertFalse(badges["AI_READY"])

    def test_status_prints_listener_hint_when_ws_offline(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            require_online=False,
        )
        diag = {"registered": True, "ws_connected": False, "last_heartbeat": 100.0, "availability": "offline"}
        with (
            patch("node.mep_runtime.MEPIdentity") as identity_cls,
            patch("node.mep_runtime.requests.request", return_value=_FakeResponse(200, diag)),
            patch("builtins.print") as print_mock,
        ):
            identity_cls.return_value.node_id = "node_test"
            code = mep_runtime.cmd_status(args)
        self.assertEqual(code, 0)
        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("listener is not running", printed)
        self.assertIn("python -m node.mep_runtime", printed)

    def test_runtime_alias_prefers_cli_then_sidecar_then_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "runtime.pem")
            mep_runtime._write_alias_sidecar(key_path, "persisted-alias")  # noqa: SLF001

            self.assertEqual(mep_runtime._resolve_runtime_alias(key_path, "cli-alias", node_id="node_fallback"), "cli-alias")  # noqa: SLF001
            self.assertEqual(mep_runtime._resolve_runtime_alias(key_path, None, node_id="node_fallback"), "persisted-alias")  # noqa: SLF001
            os.remove(mep_runtime._alias_sidecar_path(key_path))  # noqa: SLF001
            self.assertEqual(mep_runtime._resolve_runtime_alias(key_path, None, node_id="node_fallback"), "node_fallback")  # noqa: SLF001

    def test_init_persists_alias_sidecar(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            alias="fresh-node",
        )
        fake_identity = _FakeIdentity()
        fake_identity.generated_new_key = False
        fake_identity.key_path = args.key_path
        with (
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=fake_identity),
            patch("node.mep_runtime._safe_request", return_value=(200, {"balance": 10.0}, "")),
            patch("node.mep_runtime._write_alias_sidecar") as write_alias_mock,
            patch("node.mep_runtime.cmd_status", return_value=0),
        ):
            code = mep_runtime.cmd_init(args)
        self.assertEqual(code, 0)
        write_alias_mock.assert_called_once_with(args.key_path, "fresh-node")

    def test_init_defaults_alias_to_node_id_when_none_is_provided(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            alias=None,
        )
        fake_identity = _FakeIdentity()
        fake_identity.generated_new_key = False
        fake_identity.key_path = args.key_path
        with (
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=fake_identity),
            patch("node.mep_runtime._safe_request", return_value=(200, {"balance": 10.0}, "")) as request_mock,
            patch("node.mep_runtime._write_alias_sidecar") as write_alias_mock,
            patch("node.mep_runtime.cmd_status", return_value=0),
        ):
            code = mep_runtime.cmd_init(args)
        self.assertEqual(code, 0)
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["alias"], "node_runtime")
        write_alias_mock.assert_called_once_with(args.key_path, "node_runtime")

    def test_up_runs_even_if_doctor_fails(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            alias="fresh-node",
        )
        with (
            patch("node.mep_runtime.cmd_init", return_value=0) as init_mock,
            patch("node.mep_runtime.cmd_doctor", return_value=2) as doctor_mock,
            patch("node.mep_runtime.cmd_run", return_value=0) as run_mock,
        ):
            code = mep_runtime.cmd_up(args)
        self.assertEqual(code, 0)
        self.assertEqual(init_mock.call_count, 1)
        self.assertEqual(doctor_mock.call_count, 1)
        self.assertEqual(run_mock.call_count, 1)

    def test_runtime_register_includes_alias_and_x25519_public_key(self):
        node = _runtime_node()
        with patch("node.mep_runtime._safe_request", return_value=(200, {"node_id": node.node_id, "balance": 10.0}, "")) as request_mock:
            ok, _message = node.register("runtime-alias")
        self.assertTrue(ok)
        self.assertEqual(
            request_mock.call_args.kwargs["json_body"],
            {"pubkey": "pub", "alias": "runtime-alias", "x25519_public_key": "encpub"},
        )

    def test_run_reads_persisted_alias_when_cli_alias_missing(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            alias=None,
        )
        fake_runtime = _FakeRuntime()
        with (
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.mep_runtime._resolve_runtime_alias", return_value="persisted-alias") as resolve_alias_mock,
            patch("node.mep_runtime.RuntimeNode", return_value=fake_runtime) as runtime_cls,
            patch("node.mep_runtime.asyncio.run", side_effect=lambda coro: (coro.close(), 0)[1]),
        ):
            code = mep_runtime.cmd_run(args)
        self.assertEqual(code, 0)
        resolve_alias_mock.assert_called_once_with(args.key_path, None, node_id="node_runtime")
        self.assertEqual(runtime_cls.call_args.kwargs["alias"], "persisted-alias")

    def test_parser_accepts_production_runtime_adapters(self):
        parser = mep_runtime.build_parser()

        deepseek_args = parser.parse_args(["--adapter", "deepseek", "run"])
        ollama_args = parser.parse_args(["--adapter", "ollama", "status"])

        self.assertEqual(deepseek_args.adapter, "deepseek")
        self.assertEqual(ollama_args.adapter, "ollama")

    def test_run_with_deepseek_without_api_key_falls_back_to_mock_adapter(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="deepseek",
            alias="Hub-Sentinel",
        )
        fake_runtime = _FakeRuntime()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.mep_runtime._resolve_runtime_alias", return_value="Hub-Sentinel"),
            patch("node.mep_runtime.RuntimeNode", return_value=fake_runtime) as runtime_cls,
            patch("node.mep_runtime.asyncio.run", side_effect=lambda coro: (coro.close(), 0)[1]),
        ):
            code = mep_runtime.cmd_run(args)

        self.assertEqual(code, 0)
        self.assertIsInstance(runtime_cls.call_args.kwargs["adapter"], mep_runtime.MockAdapter)

    def test_run_with_deepseek_api_key_uses_deepseek_adapter(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="deepseek",
            alias="Hub-Sentinel",
        )
        fake_runtime = _FakeRuntime()
        with (
            patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret-key", "MEP_AI_MODEL": "deepseek-chat"}, clear=True),
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.mep_runtime._resolve_runtime_alias", return_value="Hub-Sentinel"),
            patch("node.mep_runtime.RuntimeNode", return_value=fake_runtime) as runtime_cls,
            patch("node.mep_runtime.asyncio.run", side_effect=lambda coro: (coro.close(), 0)[1]),
        ):
            code = mep_runtime.cmd_run(args)

        self.assertEqual(code, 0)
        adapter = runtime_cls.call_args.kwargs["adapter"]
        self.assertIsInstance(adapter, mep_runtime.DeepSeekAdapter)
        self.assertEqual(adapter.model, "deepseek-chat")


class TestRuntimeBidPolicy(unittest.TestCase):
    def test_compute_and_chat_tasks_are_bid_by_default(self):
        node = _runtime_node()

        self.assertTrue(node.should_bid({"id": "task_compute", "bounty": 1.0}))
        self.assertTrue(node.should_bid({"id": "task_chat", "bounty": 0.0}))

    def test_data_market_tasks_require_purchase_budget(self):
        with patch.dict("os.environ", {"MEP_MAX_PURCHASE_PRICE": "0.25"}):
            node = _runtime_node()

        self.assertTrue(node.should_bid({"id": "task_data_ok", "bounty": -0.25}))
        self.assertFalse(node.should_bid({"id": "task_data_expensive", "bounty": -0.5}))

    def test_data_market_tasks_are_rejected_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            node = _runtime_node()

        self.assertFalse(node.should_bid({"id": "task_data", "bounty": -0.01}))

    def test_invalid_bounty_is_not_bid(self):
        node = _runtime_node()

        self.assertFalse(node.should_bid({"id": "task_bad", "bounty": "not-a-number"}))


class TestRuntimeReviewPrompts(unittest.TestCase):
    @staticmethod
    def _bridge_review_task_data(intent_type: str = "code.review.request") -> dict:
        return {
            "id": "task_bridge_review",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge-review",
                    "trace_id": "trace-bridge-review",
                    "source": {"node_id": "node_bridge"},
                    "target": {"node_id": "node_runtime"},
                    "conversation": {"context_id": "ctx-bridge-review"},
                    "intent": {"type": intent_type, "priority": "high"},
                    "task": {
                        "instructions": "Review this PR and provide a concise decision.",
                        "inputs": {
                            "bridge_metadata": {
                                "source_type": "github",
                                "bridge_id": "br-review-123",
                                "status_endpoint": "https://bridge.example.test/bridge/status",
                                "status_token": "bridge-status-token",
                            }
                        },
                    },
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }

    def test_bridge_review_tasks_request_reviewer_prompt(self):
        self.assertTrue(mep_runtime._task_requires_review_prompt(self._bridge_review_task_data()))  # noqa: SLF001
        self.assertFalse(
            mep_runtime._task_requires_review_prompt(  # noqa: SLF001
                {"id": "task_generic", "bounty": 0.0, "payload": "hello"}
            )
        )

    def test_ai_adapter_uses_reviewer_prompt_for_bridge_review_tasks(self):
        adapter = mep_runtime.AIAdapter(model="tinyllama")
        task_data = self._bridge_review_task_data()
        with patch(
            "subprocess.run",
            return_value=_FakeCompletedProcess(
                stdout='{"summary":"Checked the provided diff.","findings":[]}'
            ),
        ) as run_mock:
            reply = adapter.generate_reply("Review this PR", task_data)

        self.assertIn("## Review Summary", reply)
        self.assertIn("Checked the provided diff.", reply)
        prompt = run_mock.call_args.args[0][3]
        self.assertIn("You are a senior code reviewer for the MEP", prompt)
        self.assertIn('"summary": string', prompt)

    def test_deepseek_adapter_uses_reviewer_prompt_for_bridge_review_tasks(self):
        adapter = mep_runtime.DeepSeekAdapter(api_key="secret-key", model="deepseek-chat")
        task_data = self._bridge_review_task_data()
        fake_response = _FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"summary":"Checked the bridge diff.","findings":'
                                '[{"file":"bridge/github_to_mep.py","issue":"Preserve coalesced targets",'
                                '"rationale":"Otherwise the second mention can overwrite the first target during the coalesce window."}]}'
                            )
                        }
                    }
                ]
            },
        )
        with patch("node.mep_runtime.requests.post", return_value=fake_response) as post_mock:
            reply = adapter.generate_reply("Review this PR", task_data)

        self.assertIn("## Review Findings", reply)
        self.assertIn("Preserve coalesced targets", reply)
        self.assertIn("bridge/github_to_mep.py", reply)
        system_prompt = post_mock.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("return ONLY a JSON object", system_prompt)

    def test_deepseek_adapter_filters_weak_review_findings(self):
        adapter = mep_runtime.DeepSeekAdapter(api_key="secret-key", model="deepseek-chat")
        task_data = self._bridge_review_task_data()
        fake_response = _FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"summary":"Missing context to verify the rest of the patch.",'
                                '"findings":[{"file":"bridge/github_to_mep.py","issue":"Need more context",'
                                '"rationale":"Cannot verify this path without seeing the full patch excerpt."}]}'
                            )
                        }
                    }
                ]
            },
        )

        with patch("node.mep_runtime.requests.post", return_value=fake_response):
            reply = adapter.generate_reply("Review this PR", task_data)

        self.assertEqual(
            reply,
            "## Review Summary\n\nReviewed the provided diff context and did not identify a concrete issue that is directly supported by the supplied patch excerpts.",
        )


class TestRuntimeWebSocketLoop(unittest.TestCase):
    def test_idle_timeout_pings_without_reconnecting(self):
        node = _runtime_node()
        task_payload = json.dumps({"event": "rfc", "data": {"id": "task_compute", "bounty": 1.0}})
        ws = _FakeWebSocket(["timeout", task_payload])

        with patch.object(node, "bid", side_effect=lambda task_id: setattr(node, "running", False)) as bid_mock:
            asyncio.run(node._recv_loop(ws))

        self.assertEqual(ws.pings, 1)
        bid_mock.assert_called_once_with("task_compute")

    def test_run_forever_cancels_background_tasks_cleanly_on_shutdown(self):
        node = _runtime_node()

        async def _pending_task() -> None:
            await asyncio.Event().wait()

        async def _recv_loop(_ws) -> None:
            node.running = False

        async def _run() -> asyncio.Task:
            with (
                patch.object(node, "register", return_value=(True, "registered")),
                patch.object(node, "_recv_loop", side_effect=_recv_loop),
                patch("node.ws_connect.ws_connect", return_value=_FakeConnectContext(_FakeWebSocket([]))),
                patch("builtins.print") as print_mock,
            ):
                node._schedule_background_task(_pending_task(), label="pending_shutdown")  # noqa: SLF001
                pending_task = next(iter(node._background_tasks))  # noqa: SLF001
                code = await node.run_forever()

            self.assertEqual(code, 0)
            self.assertTrue(pending_task.cancelled())
            self.assertFalse(node._background_tasks)  # noqa: SLF001
            printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
            self.assertNotIn("background task error", printed)
            return pending_task

        pending_task = asyncio.run(_run())
        self.assertTrue(pending_task.cancelled())

    def test_fetch_pending_tasks_uses_authenticated_get(self):
        node = _runtime_node()
        with patch("node.mep_runtime._safe_request", return_value=(200, {"tasks": [{"id": "task_pending"}]}, "")) as request_mock:
            tasks = node._fetch_pending_tasks()  # noqa: SLF001

        self.assertEqual(tasks, [{"id": "task_pending"}])
        self.assertEqual(request_mock.call_args.args, ("GET", "http://hub/tasks/pending/node_runtime"))
        self.assertEqual(request_mock.call_args.kwargs["headers"]["X-MEP-NodeID"], "node_runtime")
        self.assertEqual(node.pending_task_recovery_metrics["poll_attempts"], 1)  # noqa: SLF001
        self.assertEqual(node.pending_task_recovery_metrics["poll_successes"], 1)  # noqa: SLF001

    def test_fetch_pending_tasks_records_poll_failure_metrics(self):
        node = _runtime_node()
        with patch("node.mep_runtime._safe_request", return_value=(503, None, "hub unavailable")):
            tasks = node._fetch_pending_tasks()  # noqa: SLF001

        self.assertEqual(tasks, [])
        metrics = node.pending_task_recovery_metrics  # noqa: SLF001
        self.assertEqual(metrics["poll_attempts"], 1)
        self.assertEqual(metrics["poll_failures"], 1)
        self.assertEqual(metrics["poll_successes"], 0)
        self.assertEqual(metrics["last_poll_status"], 503)
        self.assertEqual(metrics["last_poll_failure_detail"], "hub unavailable")
        self.assertIsNotNone(metrics["last_poll_failure_at"])

    def test_fetch_pending_tasks_records_malformed_response_metrics(self):
        node = _runtime_node()
        with patch("node.mep_runtime._safe_request", return_value=(200, {"tasks": "not-a-list"}, "")):
            tasks = node._fetch_pending_tasks()  # noqa: SLF001

        self.assertEqual(tasks, [])
        metrics = node.pending_task_recovery_metrics  # noqa: SLF001
        self.assertEqual(metrics["poll_attempts"], 1)
        self.assertEqual(metrics["poll_failures"], 1)
        self.assertEqual(metrics["malformed_responses"], 1)
        self.assertEqual(metrics["poll_successes"], 0)
        self.assertEqual(metrics["last_poll_status"], 200)
        self.assertEqual(metrics["last_poll_failure_detail"], "pending task poll returned invalid tasks payload")
        self.assertIsNotNone(metrics["last_poll_failure_at"])

    def test_recover_pending_tasks_replays_new_task_events(self):
        node = _runtime_node()
        with (
            patch.object(node, "_fetch_pending_tasks", return_value=[{"id": "task_one"}, {"id": "task_two"}]),
            patch.object(node, "handle_ws_event", new=AsyncMock()) as handle_mock,
        ):
            asyncio.run(node._recover_pending_tasks())  # noqa: SLF001

        self.assertEqual(handle_mock.await_count, 2)
        self.assertEqual(handle_mock.await_args_list[0].args[0], {"event": "new_task", "data": {"id": "task_one"}})
        self.assertEqual(handle_mock.await_args_list[1].args[0], {"event": "new_task", "data": {"id": "task_two"}})
        self.assertEqual(node.pending_task_recovery_metrics["tasks_recovered"], 2)  # noqa: SLF001
        self.assertIsNotNone(node.pending_task_recovery_metrics["last_recovered_at"])  # noqa: SLF001

    def test_run_forever_recovers_pending_tasks_after_connect(self):
        node = _runtime_node()

        async def _recv_loop(_ws) -> None:
            node.running = False

        async def _run() -> int:
            with (
                patch.object(node, "register", return_value=(True, "registered")),
                patch.object(node, "_recover_pending_tasks", new=AsyncMock()) as recover_mock,
                patch.object(node, "_recv_loop", side_effect=_recv_loop),
                patch("node.ws_connect.ws_connect", return_value=_FakeConnectContext(_FakeWebSocket([]))),
            ):
                code = await node.run_forever()
                self.assertEqual(recover_mock.await_count, 1)
                return code

        self.assertEqual(asyncio.run(_run()), 0)

    def test_process_task_uses_interbot_instructions_for_adapter_input(self):
        node = _runtime_node()
        task_data = {
            "id": "task_interbot",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-1",
                    "trace_id": "trace-1",
                    "source": {"node_id": "node_peer"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-1"},
                    "intent": {"type": "chat.request", "priority": "normal"},
                    "task": {"instructions": "Use only this instruction text."},
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }
        with (
            patch.object(node.adapter, "generate_reply", return_value="reply") as adapter_mock,
            patch.object(node, "complete") as complete_mock,
        ):
            asyncio.run(node.process_task(task_data))

        adapter_mock.assert_called_once_with("Use only this instruction text.", task_data)
        complete_mock.assert_called_once_with("task_interbot", "reply")

    def test_process_task_reports_bridge_status_when_github_bridge_metadata_is_present(self):
        node = _runtime_node()
        task_data = {
            "id": "task_bridge_status",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge-status",
                    "trace_id": "trace-bridge-status",
                    "source": {"node_id": "node_bridge"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-bridge-status"},
                    "intent": {"type": "code.review.approve", "priority": "high"},
                    "task": {
                        "instructions": "Approve this PR if it looks good.",
                        "inputs": {
                            "bridge_metadata": {
                                "source_type": "github",
                                "bridge_id": "br-123",
                                "status_endpoint": "https://bridge.example.test/bridge/status",
                                "status_token": "status-token",
                            }
                        },
                    },
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }
        with (
            patch.object(node.adapter, "generate_reply", return_value="Looks good to me."),
            patch.object(node, "complete") as complete_mock,
            patch("node.mep_runtime._safe_request", return_value=(200, {}, "")) as request_mock,
        ):
            asyncio.run(node.process_task(task_data))

        complete_mock.assert_called_once_with("task_bridge_status", "Looks good to me.")
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.args, ("POST", "https://bridge.example.test/bridge/status"))
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["bridge_id"], "br-123")
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["status"], "completed")
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["action"], "approved")
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["detail"], "Looks good to me.")
        self.assertEqual(request_mock.call_args.kwargs["headers"]["Authorization"], "Bearer status-token")

    def test_process_task_live_bridge_sends_frame_and_settles_when_call_is_accepted(self):
        node = _runtime_node()
        node.live_call_enabled = True
        node.dm_to_call_bridge_enabled = True
        node._ws = _FakeWebSocket([])
        node.call_invite_timeout_ms = 100

        task_data = {
            "id": "task_bridge",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge",
                    "trace_id": "trace-bridge",
                    "source": {"node_id": "node_peer"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-bridge"},
                    "intent": {"type": "chat.request", "priority": "normal"},
                    "task": {"instructions": "Reply over live call."},
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }

        async def _run() -> None:
            with (
                patch.object(node.adapter, "generate_reply", return_value="Live reply"),
                patch.object(node, "complete") as complete_mock,
            ):
                task = asyncio.create_task(node.process_task(task_data))
                await asyncio.sleep(0)
                invite = json.loads(node._ws.sent[0])
                self.assertEqual(invite["event"], "call.invite")
                self.assertEqual(invite["context_id"], "ctx-bridge")
                self.assertEqual(invite["callee"], "node_peer")

                await node.handle_ws_event({"event": "call.accepted", "context_id": "ctx-bridge"})
                await task

                events = [json.loads(payload)["event"] for payload in node._ws.sent]
                self.assertEqual(events, ["call.invite", "call.frame", "call.hangup"])
                complete_mock.assert_called_once()
                settled_payload = complete_mock.call_args.args[1]
                self.assertIn("LIVE_CALL_BRIDGE_OK", settled_payload)
                self.assertIn("context=ctx-bridge", settled_payload)

        asyncio.run(_run())

    def test_process_task_live_bridge_falls_back_when_frame_send_fails_after_accept(self):
        node = _runtime_node()
        node.live_call_enabled = True
        node.dm_to_call_bridge_enabled = True
        node._ws = _FakeWebSocket([])
        node.call_invite_timeout_ms = 100

        task_data = {
            "id": "task_bridge_frame_fail",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge-frame-fail",
                    "trace_id": "trace-bridge-frame-fail",
                    "source": {"node_id": "node_peer"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-bridge-frame-fail"},
                    "intent": {"type": "chat.request", "priority": "normal"},
                    "task": {"instructions": "Reply over live call unless the frame send fails."},
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }

        original_send_ws_event = node._send_ws_event

        async def _send_ws_event(payload):
            if payload.get("event") == "call.frame":
                return False
            return await original_send_ws_event(payload)

        async def _run() -> None:
            with (
                patch.object(node.adapter, "generate_reply", return_value="Fallback after frame failure"),
                patch.object(node, "_send_ws_event", side_effect=_send_ws_event) as send_mock,
                patch.object(node, "complete") as complete_mock,
            ):
                task = asyncio.create_task(node.process_task(task_data))
                await asyncio.sleep(0)
                await node.handle_ws_event({"event": "call.accepted", "context_id": "ctx-bridge-frame-fail"})
                await task

                self.assertEqual(json.loads(node._ws.sent[0])["event"], "call.invite")
                self.assertEqual(send_mock.await_count, 2)
                complete_mock.assert_called_once_with("task_bridge_frame_fail", "Fallback after frame failure")

        asyncio.run(_run())

    def test_process_task_live_bridge_falls_back_to_task_result_when_call_is_declined(self):
        node = _runtime_node()
        node.live_call_enabled = True
        node.dm_to_call_bridge_enabled = True
        node._ws = _FakeWebSocket([])
        node.call_invite_timeout_ms = 100

        task_data = {
            "id": "task_bridge_declined",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge-declined",
                    "trace_id": "trace-bridge-declined",
                    "source": {"node_id": "node_peer"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-bridge-declined"},
                    "intent": {"type": "chat.request", "priority": "normal"},
                    "task": {"instructions": "Reply over live call if possible."},
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }

        async def _run() -> None:
            with (
                patch.object(node.adapter, "generate_reply", return_value="Fallback reply"),
                patch.object(node, "complete") as complete_mock,
            ):
                task = asyncio.create_task(node.process_task(task_data))
                await asyncio.sleep(0)
                await node.handle_ws_event(
                    {"event": "call.declined", "context_id": "ctx-bridge-declined", "reason": "busy"}
                )
                await task

                events = [json.loads(payload)["event"] for payload in node._ws.sent]
                self.assertEqual(events, ["call.invite"])
                complete_mock.assert_called_once_with("task_bridge_declined", "Fallback reply")

        asyncio.run(_run())

    def test_process_task_live_bridge_decline_falls_back_to_bounded_structured_dm_reply(self):
        node = _runtime_node()
        node.live_call_enabled = True
        node.dm_to_call_bridge_enabled = True
        node._ws = _FakeWebSocket([])
        node.call_invite_timeout_ms = 100

        task_data = {
            "id": "task_bridge_structured",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge-structured",
                    "trace_id": "trace-bridge-structured",
                    "source": {"node_id": "node_peer"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-bridge-structured", "turn_type": "review_request", "turn_index": 1},
                    "intent": {"type": "review.request", "priority": "high"},
                    "task": {
                        "instructions": "Reply over live call if possible, otherwise stay in the bounded DM thread.",
                        "inputs": {"session_safety": {"max_turns": 4, "checkpoint_interval": 3}},
                    },
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }

        async def _run() -> None:
            with (
                patch.object(node.adapter, "generate_reply", return_value="Structured fallback reply"),
                patch.object(
                    node,
                    "_submit_structured_interbot_message",
                    return_value=(True, {"task_id": "task_reply_out"}, ""),
                ) as submit_mock,
                patch.object(node, "complete") as complete_mock,
            ):
                task = asyncio.create_task(node.process_task(task_data))
                await asyncio.sleep(0)
                await node.handle_ws_event(
                    {"event": "call.declined", "context_id": "ctx-bridge-structured", "reason": "busy"}
                )
                await task

                envelope = submit_mock.call_args.args[0]
                self.assertEqual(envelope["target"]["node_id"], "node_peer")
                self.assertEqual(envelope["task"]["instructions"], "Structured fallback reply")
                self.assertEqual(envelope["conversation"]["context_id"], "ctx-bridge-structured")
                self.assertEqual(envelope["conversation"]["reply_to_task_id"], "task_bridge_structured")
                self.assertEqual(envelope["conversation"]["turn_type"], "review_response")
                self.assertEqual(envelope["conversation"]["turn_index"], 2)
                complete_mock.assert_called_once()
                settled_payload = complete_mock.call_args.args[1]
                self.assertIn("DM_REPLY_SENT", settled_payload)
                self.assertIn("reply_task=task_reply_out", settled_payload)

        asyncio.run(_run())

    def test_process_task_stops_bounded_structured_dm_reply_when_session_limit_is_exceeded(self):
        node = _runtime_node()
        task_data = {
            "id": "task_stop_structured",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-stop-structured",
                    "trace_id": "trace-stop-structured",
                    "source": {"node_id": "node_peer"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-stop-structured", "turn_type": "chat_turn", "turn_index": 1},
                    "intent": {"type": "chat.request", "priority": "normal"},
                    "task": {
                        "instructions": "This thread is already at its max turn budget.",
                        "inputs": {"session_safety": {"max_turns": 1}},
                    },
                    "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
                    "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
                }
            ),
        }

        with (
            patch.object(node.adapter, "generate_reply", return_value="Should not be sent"),
            patch.object(node, "_submit_structured_interbot_message") as submit_mock,
            patch.object(node, "complete") as complete_mock,
        ):
            asyncio.run(node.process_task(task_data))

        submit_mock.assert_not_called()
        complete_mock.assert_called_once()
        settled_payload = complete_mock.call_args.args[1]
        self.assertIn("DM_REPLY_STOPPED", settled_payload)
        self.assertIn("reason=max_turns_exceeded", settled_payload)

    def test_runtime_auto_accepts_incoming_live_call_when_enabled(self):
        node = _runtime_node()
        node.live_call_enabled = True
        node.call_auto_accept = True
        node._ws = _FakeWebSocket([])

        asyncio.run(node.handle_ws_event({"event": "call.incoming", "context_id": "ctx-auto", "caller": "node_peer"}))

        self.assertEqual([json.loads(payload)["event"] for payload in node._ws.sent], ["call.accept"])


class TestRuntimeKeyDirResolution(unittest.TestCase):
    def test_find_git_root_detects_worktree_dot_git_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = os.path.join(tmpdir, "wt")
            nested = os.path.join(repo, "a", "b")
            os.makedirs(nested)
            # git worktrees/submodules use a .git *file* pointing elsewhere,
            # not a .git directory.
            with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as handle:
                handle.write("gitdir: /elsewhere/.git/worktrees/wt\n")
            self.assertEqual(
                os.path.realpath(mep_runtime._find_git_root(nested)),  # noqa: SLF001
                os.path.realpath(repo),
            )

    def test_default_key_path_prefers_explicit_env(self):
        with patch.dict(os.environ, {"MEP_PROVIDER_KEY_PATH": "/custom/key.pem"}, clear=False):
            self.assertEqual(mep_runtime._default_key_path(), "/custom/key.pem")  # noqa: SLF001

    def test_default_key_dir_prefers_explicit_env(self):
        with patch.dict(os.environ, {"MEP_KEY_DIR": "/custom/dir"}, clear=False):
            self.assertEqual(mep_runtime._default_key_dir(), "/custom/dir")  # noqa: SLF001

    def test_key_path_resolved_lazily_when_flag_omitted(self):
        with patch.dict(os.environ, {"MEP_PROVIDER_KEY_PATH": "/lazy/key.pem"}, clear=False):
            with patch.object(mep_runtime, "cmd_status", return_value=0) as status_mock:
                mep_runtime.main(["status"])
        args = status_mock.call_args.args[0]
        self.assertEqual(args.key_path, "/lazy/key.pem")

    def test_create_new_local_identity_uses_node_id_canonical_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = mep_runtime._create_new_local_identity(tmpdir)  # noqa: SLF001
            identity = MEPIdentity(key_path)
            self.assertEqual(os.path.basename(key_path), f"{identity.node_id}.pem")
            self.assertTrue(os.path.exists(key_path))
            self.assertTrue(os.path.exists(key_path.replace(".pem", "_enc.pem")))

    def test_choose_existing_local_identity_migrates_legacy_runtime_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_path = os.path.join(tmpdir, "mep_runtime.pem")
            identity = MEPIdentity(legacy_path)
            mep_runtime._write_alias_sidecar(legacy_path, "legacy-node")  # noqa: SLF001

            selected = mep_runtime._choose_existing_local_identity(tmpdir, None)  # noqa: SLF001

            canonical_path = os.path.join(tmpdir, f"{identity.node_id}.pem")
            self.assertEqual(selected, canonical_path)
            self.assertTrue(os.path.exists(canonical_path))
            self.assertFalse(os.path.exists(legacy_path))
            self.assertEqual(mep_runtime._read_alias_sidecar(canonical_path), "legacy-node")  # noqa: SLF001

    def test_choose_existing_local_identity_uses_alias_to_disambiguate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            alpha = os.path.join(tmpdir, "alpha.pem")
            beta = os.path.join(tmpdir, "beta.pem")
            beta_id = MEPIdentity(beta)
            MEPIdentity(alpha)
            mep_runtime._write_alias_sidecar(alpha, "alpha-bot")  # noqa: SLF001
            mep_runtime._write_alias_sidecar(beta, "beta-bot")  # noqa: SLF001

            selected = mep_runtime._choose_existing_local_identity(tmpdir, "beta-bot")  # noqa: SLF001

            self.assertEqual(selected, os.path.join(tmpdir, f"{beta_id.node_id}.pem"))
            self.assertTrue(os.path.exists(alpha))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, f"{beta_id.node_id}.pem")))

    def test_choose_existing_local_identity_rejects_ambiguous_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            MEPIdentity(os.path.join(tmpdir, "alpha.pem"))
            MEPIdentity(os.path.join(tmpdir, "beta.pem"))

            with self.assertRaises(mep_runtime.RuntimeKeyPathError):
                mep_runtime._choose_existing_local_identity(tmpdir, None)  # noqa: SLF001

    def test_choose_existing_local_identity_rejects_single_alias_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            only_key = os.path.join(tmpdir, "only.pem")
            MEPIdentity(only_key)
            mep_runtime._write_alias_sidecar(only_key, "alpha-bot")  # noqa: SLF001

            with self.assertRaisesRegex(mep_runtime.RuntimeKeyPathError, "matches alias"):
                mep_runtime._choose_existing_local_identity(tmpdir, "beta-bot")  # noqa: SLF001

    def test_main_rejects_run_without_key_when_identity_selection_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(mep_runtime, "_default_key_dir", return_value=tmpdir):
                MEPIdentity(os.path.join(tmpdir, "alpha.pem"))
                MEPIdentity(os.path.join(tmpdir, "beta.pem"))

                with patch("builtins.print") as print_mock:
                    code = mep_runtime.main(["run"])

            self.assertEqual(code, 2)
            printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
            self.assertIn("multiple local identities found", printed)


if __name__ == "__main__":
    unittest.main()
