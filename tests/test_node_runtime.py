import argparse
import asyncio
import json
import os
import tempfile
import unittest
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

from clients.shared import review_patterns
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
    key_path = "fake.pem"

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
        openai_args = parser.parse_args(["--adapter", "openai", "run"])

        self.assertEqual(deepseek_args.adapter, "deepseek")
        self.assertEqual(ollama_args.adapter, "ollama")
        self.assertEqual(openai_args.adapter, "openai")

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

    def test_run_with_strict_deepseek_without_api_key_fails_closed(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="deepseek",
            alias="Hub-Sentinel",
        )
        with (
            patch.dict("os.environ", {"MEP_STRICT_ADAPTERS": "true"}, clear=True),
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.RuntimeNode") as runtime_cls,
        ):
            code = mep_runtime.cmd_run(args)

        self.assertEqual(code, 2)
        runtime_cls.assert_not_called()

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

    def test_run_with_openai_without_config_falls_back_to_mock_adapter(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="openai",
            alias="Elsaws Bot",
        )
        fake_runtime = _FakeRuntime()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.mep_runtime._resolve_runtime_alias", return_value="Elsaws Bot"),
            patch("node.mep_runtime.RuntimeNode", return_value=fake_runtime) as runtime_cls,
            patch("node.mep_runtime.asyncio.run", side_effect=lambda coro: (coro.close(), 0)[1]),
        ):
            code = mep_runtime.cmd_run(args)

        self.assertEqual(code, 0)
        self.assertIsInstance(runtime_cls.call_args.kwargs["adapter"], mep_runtime.MockAdapter)

    def test_run_with_strict_openai_without_config_fails_closed(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="openai",
            alias="Elsaws Bot",
        )
        with (
            patch.dict("os.environ", {"MEP_STRICT_ADAPTERS": "true"}, clear=True),
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.RuntimeNode") as runtime_cls,
        ):
            code = mep_runtime.cmd_run(args)

        self.assertEqual(code, 2)
        runtime_cls.assert_not_called()

    def test_run_with_openai_config_uses_openai_compatible_adapter(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            ws_url="ws://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="openai",
            alias="Elsaws Bot",
        )
        fake_runtime = _FakeRuntime()
        with (
            patch.dict(
                "os.environ",
                {
                    "MIMO_API_KEY": "secret-key",
                    "OPENAI_COMPAT_BASE_URL": "https://api.xiaomimimo.com/v1",
                    "MEP_AI_MODEL": "mimo-v2.5-pro",
                    "OPENAI_COMPAT_PROVIDER_NAME": "mimo",
                },
                clear=True,
            ),
            patch("node.mep_runtime._ensure_key_parent"),
            patch("node.mep_runtime.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.mep_runtime._resolve_runtime_alias", return_value="Elsaws Bot"),
            patch("node.mep_runtime.RuntimeNode", return_value=fake_runtime) as runtime_cls,
            patch("node.mep_runtime.asyncio.run", side_effect=lambda coro: (coro.close(), 0)[1]),
        ):
            code = mep_runtime.cmd_run(args)

        self.assertEqual(code, 0)
        adapter = runtime_cls.call_args.kwargs["adapter"]
        self.assertIsInstance(adapter, mep_runtime.OpenAICompatibleAdapter)
        self.assertEqual(adapter.model, "mimo-v2.5-pro")
        self.assertEqual(adapter.base_url, "https://api.xiaomimimo.com/v1")
        self.assertEqual(adapter.provider_name, "mimo")


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
    def _bridge_review_task_data(
        intent_type: str = "code.review.request",
        *,
        changed_identifiers: Optional[list[str]] = None,
        touched_paths: Optional[list[str]] = None,
        touched_tests: Optional[list[str]] = None,
        changed_files: Optional[list[dict[str, Any]]] = None,
        review_mode: str = "discovery_review",
        ci_checks: Optional[dict[str, Any]] = None,
        runtime_tool_bundle: Optional[dict[str, Any]] = None,
    ) -> dict:
        identifiers = changed_identifiers or [
            "_record_pending_task_poll_failure",
            "last_poll_status",
        ]
        github_touched_paths = touched_paths or ["bridge/github_to_mep.py"]
        github_touched_tests = touched_tests if touched_tests is not None else ["tests/test_github_bridge.py"]
        bundle = runtime_tool_bundle or {
            "contract_version": "mep.runtime_tools.v1",
            "task_mode": "review",
            "runs": [
                {
                    "tool": "github_context",
                    "purpose": "Enrich PR metadata",
                    "status": "success",
                    "summary": "assembled normalized GitHub PR context for the current review task",
                    "scope": "pr_review",
                    "evidence": ["WUAIBING/MEP#246", "feature/test"],
                },
                {
                    "tool": "workspace_read",
                    "purpose": "Read the checked-out PR workspace",
                    "status": "success",
                    "summary": "assembled authoritative local review context from the synced PR workspace",
                    "scope": "pr_review",
                    "evidence": ["bridge/github_to_mep.py", "tests/test_github_bridge.py"],
                },
                {
                    "tool": "workspace_search",
                    "purpose": "Expand touched code to nearby identifiers and call sites",
                    "status": "success",
                    "summary": "searched the synced PR workspace for changed identifiers and nearby evidence",
                    "scope": "pr_review",
                    "evidence": identifiers[:2],
                },
                {
                    "tool": "workspace_git",
                    "purpose": "Anchor review evidence to the exact checked-out workspace state",
                    "status": "success",
                    "summary": "captured git head and tracked-path state for the synced PR workspace",
                    "scope": "pr_review",
                    "evidence": ["bridge/github_to_mep.py"],
                },
            ],
        }
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
                            },
                            "github": {
                                "repo_full_name": "WUAIBING/MEP",
                                "entity_type": "pr",
                                "number": 246,
                                "title": "Tighten bridge review grounding",
                                "body": "Improve PR review grounding and approval behavior.",
                                "head_ref": "feature/test",
                                "base_ref": "main",
                                "review_mode": review_mode,
                                "touched_paths": github_touched_paths,
                                "touched_tests": github_touched_tests,
                                "changed_files": changed_files or [],
                                "ci_checks": ci_checks or {"has_checks": False, "state": "none", "all_green": False},
                                "risk_pack": {
                                    "changed_identifiers": identifiers
                                },
                            },
                            "runtime_tool_bundle": bundle,
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
                stdout=(
                    '{"summary":"Checked the provided diff.","observation":"The changed path is narrow and test-backed.",'
                    '"touched_paths":["bridge/github_to_mep.py"],'
                    '"tests_reviewed":["tests/test_github_bridge.py"],'
                    '"findings":[],"approval_recommendation":"approve"}'
                )
            ),
        ) as run_mock:
            reply = adapter.generate_reply("Review this PR", task_data)

        self.assertIn("## Review Summary", reply)
        self.assertIn("Checked the provided diff.", reply)
        self.assertIn("Observation: The changed path is narrow and test-backed.", reply)
        self.assertIn("Touched paths reviewed: `bridge/github_to_mep.py`", reply)
        self.assertIn("Tests reviewed: `tests/test_github_bridge.py`", reply)
        prompt = run_mock.call_args.args[0][3]
        self.assertIn("You are a senior code reviewer for the MEP", prompt)
        self.assertIn('"summary": string', prompt)
        self.assertIn('"observation": string', prompt)
        self.assertIn('"touched_paths": [string]', prompt)
        self.assertIn('"tests_reviewed": [string]', prompt)
        self.assertIn('"risk_areas_checked": [string]', prompt)
        self.assertIn('"checks_performed": [string]', prompt)
        self.assertNotIn('"why_no_finding": string', prompt)
        self.assertIn('"approval_recommendation"', prompt)
        self.assertIn("highest-value correctness, regression, edge-case", prompt)
        self.assertIn("always anchor the output to the actual diff", prompt)
        self.assertIn("Review mode is `discovery_review`.", prompt)

    def test_approval_review_prompt_requires_verified_identifiers_and_test_awareness(self):
        task_data = self._bridge_review_task_data(intent_type="code.review.approve")
        prompt = mep_runtime._system_prompt_for_task(  # noqa: SLF001
            task_data,
            generic_max_chars=300,
            review_max_chars=1000,
        )

        self.assertIn('"verified_identifiers": [string]', prompt)
        self.assertIn("Approval mode is active.", prompt)
        self.assertIn("at least two exact identifiers", prompt)
        self.assertIn("mention the changed tests", prompt)
        self.assertIn("state the scope is low-risk", prompt)
        self.assertIn("checks are pending or failing", prompt)
        self.assertIn("Diff restatement without risk coverage is not a sufficient review", prompt)

    def test_recheck_review_prompt_mentions_follow_up_verification_mode(self):
        task_data = self._bridge_review_task_data(review_mode="recheck_review")
        prompt = mep_runtime._system_prompt_for_task(  # noqa: SLF001
            task_data,
            generic_max_chars=300,
            review_max_chars=1000,
        )

        self.assertIn("Review mode is `recheck_review`.", prompt)
        self.assertIn("follow-up verification pass", prompt)
        self.assertIn("Do not invent fresh low-signal concerns", prompt)

    def test_review_prompt_escalates_deep_review_for_triggered_paths(self):
        task_data = self._bridge_review_task_data(
            touched_paths=["hub/db.py"],
            changed_identifiers=["get_pub_pem", "connected_nodes"],
        )
        prompt = mep_runtime._system_prompt_for_task(  # noqa: SLF001
            task_data,
            generic_max_chars=300,
            review_max_chars=1000,
        )

        self.assertIn("Deep-review escalation is active", prompt)
        self.assertIn("call sites", prompt)
        self.assertIn("`get_pub_pem`", prompt)

    def test_review_prompt_blocks_summary_claims_that_ignore_nearby_validation_guards(self):
        prompt = mep_runtime._system_prompt_for_task(  # noqa: SLF001
            self._bridge_review_task_data(),
            generic_max_chars=300,
            review_max_chars=1000,
        )

        self.assertIn("Do not claim a helper is missing validation, checks, or guards", prompt)
        self.assertIn("nearby allowlist, raise, or verification branch", prompt)

    def test_review_prompt_blocks_hashability_claims_from_plain_allowlist_membership(self):
        prompt = mep_runtime._system_prompt_for_task(  # noqa: SLF001
            self._bridge_review_task_data(),
            generic_max_chars=300,
            review_max_chars=1000,
        )

        self.assertIn("Do not publish a runtime-exception finding", prompt)
        self.assertIn("Do not infer `TypeError`, `unhashable`", prompt)
        self.assertIn("allowlist or classification set", prompt)

    def test_review_lenses_include_bridge_safety_focus(self):
        lenses = mep_runtime._review_lenses_for_task(self._bridge_review_task_data())  # noqa: SLF001

        self.assertIn("correctness/regression around the changed behavior", lenses)
        self.assertIn("test alignment and edge-case coverage for the changed behavior", lenses)
        self.assertIn("security/trust-boundary regressions and approval safety", lenses)
        self.assertIn("automation/writeback safety and path-to-action mismatches", lenses)

    def test_deepseek_adapter_uses_reviewer_prompt_for_bridge_review_tasks(self):
        adapter = mep_runtime.DeepSeekAdapter(api_key="secret-key", model="deepseek-chat")
        task_data = self._bridge_review_task_data(
            changed_identifiers=["preserve_coalesced_targets", "coalesced_targets"]
        )
        fake_responses = [
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"risk_candidates":['
                                    '{"file":"bridge/github_to_mep.py","claim":"Coalesced targets can be overwritten during the window.",'
                                    '"category":"automation/writeback safety","priority":"high",'
                                    '"reason":"The candidate pass noticed multiple target writes in the same bridge flow.",'
                                    '"evidence":["preserve_coalesced_targets","coalesced_targets"]}'
                                    '],"coverage":["bridge metadata coalescing"]}'
                                )
                            }
                        }
                    ]
                },
            ),
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"summary":"Checked the bridge diff.","observation":"The metadata wiring stays backward-compatible.",'
                                    '"touched_paths":["bridge/github_to_mep.py"],'
                                    '"tests_reviewed":["tests/test_github_bridge.py"],'
                                    '"verified_identifiers":["preserve_coalesced_targets"],'
                                    '"findings":'
                                    '[{"file":"bridge/github_to_mep.py","issue":"Preserve coalesced targets",'
                                    '"rationale":"Otherwise the second mention can overwrite the first target during the coalesce window."}],'
                                    '"approval_recommendation":"comment"}'
                                )
                            }
                        }
                    ]
                },
            ),
        ]
        with patch("node.mep_runtime.requests.post", side_effect=fake_responses) as post_mock:
            reply = adapter.generate_reply("Review this PR", task_data)

        self.assertIn("## Review Findings", reply)
        self.assertIn("Preserve coalesced targets", reply)
        self.assertIn("bridge/github_to_mep.py", reply)
        self.assertIn("Observation: The metadata wiring stays backward-compatible.", reply)
        self.assertIn("Tests reviewed: `tests/test_github_bridge.py`", reply)
        self.assertEqual(post_mock.call_count, 2)
        first_system_prompt = post_mock.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        first_user_payload = post_mock.call_args_list[0].kwargs["json"]["messages"][1]["content"]
        second_system_prompt = post_mock.call_args_list[1].kwargs["json"]["messages"][0]["content"]
        second_user_payload = post_mock.call_args_list[1].kwargs["json"]["messages"][1]["content"]
        self.assertIn("candidate-generation pass", first_system_prompt)
        self.assertIn("Prefer at most one candidate per lens", first_system_prompt)
        self.assertIn("highest-impact candidate first", first_system_prompt)
        self.assertIn("Review lenses to cover before publishing", first_user_payload)
        self.assertIn("return ONLY a JSON object", second_system_prompt)
        self.assertIn("This is the verification pass.", second_system_prompt)
        self.assertIn("set `file` to one of the supplied touched paths", second_system_prompt)
        self.assertIn("exact changed-line identifier in `verified_identifiers`", second_system_prompt)
        self.assertIn("single highest-impact verified finding", second_system_prompt)
        self.assertIn("Review lenses to cover before publishing", second_user_payload)
        self.assertIn("Candidate risks to verify before publishing any finding", second_user_payload)

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

        self.assertIn("## Review Summary", reply)
        self.assertIn("Touched paths reviewed: `bridge/github_to_mep.py`", reply)
        self.assertIn("Tests reviewed: `tests/test_github_bridge.py`", reply)
        self.assertIn("Risk areas checked:", reply)
        self.assertIn("Checks performed:", reply)
        self.assertIn("Observation:", reply)
        self.assertNotIn("Why no finding:", reply)

    def test_structured_review_falls_back_to_github_inputs_for_paths_and_tests(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            '{"summary":"Checked the bridge diff.","observation":"The diff is small and scoped.","findings":[]}',
            max_chars=1000,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("Observation: The diff is small and scoped.", rendered)
        self.assertIn("Touched paths reviewed: `bridge/github_to_mep.py`", rendered)
        self.assertIn("Tests reviewed: `tests/test_github_bridge.py`", rendered)

    def test_structured_approval_review_renders_verified_identifiers(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Verified the approval gate stays low-risk.",'
                '"observation":"`_score_review_quality` and `_approval_quality_failure` now enforce changed-line and test-aware approvals.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["_score_review_quality","_approval_quality_failure"],'
                '"findings":[],"approval_recommendation":"approve"}'
            ),
            max_chars=1400,
            task_data=self._bridge_review_task_data(
                intent_type="code.review.approve",
                changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertIn("Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`", rendered)
        self.assertIn("Tests reviewed: `tests/test_github_bridge.py`", rendered)

    def test_structured_approval_review_replaces_behavior_overstatement_with_grounded_defaults(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"The snippet-grounding change looks correct and well-tested.",'
                '"observation":"`_extract_backticked_review_snippets` strips and lowercases text before extraction, and `_split_review_section_items` uses `in_backticks` to avoid comma drift.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["_split_review_section_items","_extract_backticked_review_snippets"],'
                '"findings":[],"approval_recommendation":"approve"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(
                intent_type="code.review.approve",
                changed_identifiers=["_split_review_section_items", "_extract_backticked_review_snippets"],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertNotIn("strips and lowercases text before extraction", rendered)
        self.assertIn(
            "Reviewed the changed behavior around `_split_review_section_items`, `_extract_backticked_review_snippets` and did not find a concrete issue supported by the diff.",
            rendered,
        )
        self.assertIn(
            "Observation: `_split_review_section_items`, `_extract_backticked_review_snippets` stay scoped to `bridge/github_to_mep.py`, and the changed test context in `tests/test_github_bridge.py` supports the reviewed low-risk path.",
            rendered,
        )

    def test_structured_review_omits_context_only_verified_identifiers_for_comment_only_change(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked `_add_trigger` and found no blocker.",'
                '"observation":"`_add_trigger` stays scoped to `node/mep_runtime.py`.",'
                '"touched_paths":["node/mep_runtime.py"],'
                '"tests_reviewed":["tests/test_node_runtime.py"],'
                '"verified_identifiers":["_add_trigger"],'
                '"findings":[]}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["_add_trigger"],
                touched_paths=["node/mep_runtime.py"],
                touched_tests=["tests/test_node_runtime.py"],
                changed_files=[
                    {
                        "filename": "node/mep_runtime.py",
                        "status": "modified",
                        "patch_excerpt": "+    # Some tokens intentionally overlap across trust buckets.\n",
                    },
                    {
                        "filename": "tests/test_node_runtime.py",
                        "status": "modified",
                        "patch_excerpt": "+def test_comment_only_grounding(self):\n+    assert True\n",
                    },
                ],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertIn("Touched paths reviewed: `node/mep_runtime.py`", rendered)
        self.assertIn("Tests reviewed: `tests/test_node_runtime.py`", rendered)
        self.assertNotIn("Changed identifiers verified:", rendered)
        self.assertNotIn("_add_trigger` stays scoped", rendered)

    def test_approval_bridge_action_downgrades_when_rendered_review_still_has_findings(self):
        task_data = self._bridge_review_task_data(
            intent_type="code.review.approve",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
        )
        detail = (
            "## Review Findings\n\n"
            "The approval path still has a blocker.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`\n\n"
            "1. **Keep approval gated** (`bridge/github_to_mep.py`): A finding still survives verification."
        )

        action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            mep_runtime._interbot_message_from_task_data(task_data),  # noqa: SLF001
            detail=detail,
            task_data=task_data,
        )

        self.assertEqual(action, "reviewed")

    def test_approval_bridge_action_keeps_approved_for_grounded_no_finding_review(self):
        task_data = self._bridge_review_task_data(
            intent_type="code.review.approve",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
        )
        detail = (
            "## Review Summary\n\n"
            "Verified the approval gate stays low-risk.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )

        action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            mep_runtime._interbot_message_from_task_data(task_data),  # noqa: SLF001
            detail=detail,
            task_data=task_data,
        )

        self.assertEqual(action, "approved")

    def test_approval_bridge_action_downgrades_when_runtime_tool_evidence_is_weak(self):
        task_data = self._bridge_review_task_data(
            intent_type="code.review.approve",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
            runtime_tool_bundle={
                "contract_version": "mep.runtime_tools.v1",
                "task_mode": "review",
                "runs": [
                    {
                        "tool": "workspace_read",
                        "status": "success",
                        "summary": "assembled local context",
                        "evidence": ["bridge/github_to_mep.py"],
                    }
                ],
            },
        )
        detail = (
            "## Review Summary\n\n"
            "Verified the approval gate stays low-risk.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )

        action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            mep_runtime._interbot_message_from_task_data(task_data),  # noqa: SLF001
            detail=detail,
            task_data=task_data,
        )

        self.assertEqual(action, "reviewed")

    def test_recheck_review_request_bridge_action_upgrades_to_approved_for_grounded_no_finding_review(self):
        task_data = self._bridge_review_task_data(
            intent_type="code.review.request",
            review_mode="recheck_review",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
        )
        detail = (
            "## Review Summary\n\n"
            "Verified the follow-up patch stays low-risk and the earlier concern is resolved.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )

        action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            mep_runtime._interbot_message_from_task_data(task_data),  # noqa: SLF001
            detail=detail,
            task_data=task_data,
        )

        self.assertEqual(action, "approved")

    def test_discovery_review_request_bridge_action_upgrades_to_approved_for_grounded_no_finding_review(self):
        task_data = self._bridge_review_task_data(
            intent_type="code.review.request",
            review_mode="discovery_review",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
        )
        detail = (
            "## Review Summary\n\n"
            "Checked the initial review pass and found no blocker.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )

        action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            mep_runtime._interbot_message_from_task_data(task_data),  # noqa: SLF001
            detail=detail,
            task_data=task_data,
        )

        self.assertEqual(action, "approved")

    def test_structured_review_renders_risk_coverage_for_no_finding_reviews(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the review trial ledger flow end to end.",'
                '"observation":"`_build_review_trial_result` and `list_review_trials` stay aligned with the stored JSON payload.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"risk_areas_checked":["trial persistence","endpoint decoding"],'
                '"checks_performed":["verified suppression and publish paths mention `review_result_json`","checked `/bridge/review-trials` returns stored review metadata"],'
                '"why_no_finding":"The new writes and reads stay consistent across both persistence paths, so the telemetry path looks low-risk.",'
                '"verified_identifiers":["_build_review_trial_result","list_review_trials"],'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["_build_review_trial_result", "list_review_trials"]
            ),
        )

        self.assertIn("Risk areas checked: trial persistence, endpoint decoding", rendered)
        self.assertIn("Checks performed: reviewed the changed diff for `bridge/github_to_mep.py`, verified changed identifiers `_build_review_trial_result`, `list_review_trials` against the supplied review context, checked relevant changed tests `tests/test_github_bridge.py`", rendered)
        self.assertNotIn("Why no finding:", rendered)

    def test_structured_review_replaces_model_checks_with_grounded_defaults_for_no_finding_reviews(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the review trial ledger flow end to end.",'
                '"observation":"`_build_review_trial_result` and `list_review_trials` stay aligned with the stored JSON payload.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"risk_areas_checked":["trial persistence","endpoint decoding"],'
                '"checks_performed":["Confirmed that `imagined_guard` keeps the publish path safe for retries"],'
                '"verified_identifiers":["_build_review_trial_result","list_review_trials"],'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["_build_review_trial_result", "list_review_trials"]
            ),
        )

        self.assertNotIn("imagined_guard", rendered)
        self.assertIn("reviewed the changed diff for `bridge/github_to_mep.py`", rendered)
        self.assertIn("verified changed identifiers `_build_review_trial_result`, `list_review_trials` against the supplied review context", rendered)

    def test_clean_review_label_drops_partial_trailing_word_when_clipped(self):
        cleaned = mep_runtime._clean_review_label(  # noqa: SLF001
            "Confirmed that _filter_review_list_to_allowed uses exact matching and avoids trailing filteri",
            max_chars=88,
        )

        self.assertNotIn("filteri", cleaned)
        self.assertTrue(cleaned.endswith("trailing"))

    def test_clean_review_label_drops_short_trailing_fragment_when_clipped(self):
        cleaned = mep_runtime._clean_review_label(  # noqa: SLF001
            "Checked that _clip_without_partial_token clips text without ensuring token boundaries, but ca",
            max_chars=92,
        )

        self.assertNotIn("but ca", cleaned)
        self.assertTrue(cleaned.endswith("boundaries"))

    def test_clean_review_label_drops_dangling_single_letter_tail_without_local_clip(self):
        cleaned = mep_runtime._clean_review_label(  # noqa: SLF001
            "Confirmed that the publish path stays grounded and the verification trail remains safe f",
            max_chars=120,
        )

        self.assertNotIn("safe f", cleaned)
        self.assertTrue(cleaned.endswith("safe"))

    def test_finalize_model_reply_drops_partial_trailing_word_after_clip(self):
        rendered = mep_runtime._finalize_model_reply(  # noqa: SLF001
            "## Review Summary\n\nChecks performed: confirmed the publish path stays grounded and avoids trailing filteri",
            max_chars=96,
        )

        self.assertNotIn("filteri", rendered)
        self.assertNotIn("filter.", rendered)

    def test_structured_review_fills_no_finding_defaults_from_task_data(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"The replay-protection and validation changes look scoped and coherent.",'
                '"touched_paths":["hub/auth.py","hub/db.py","hub/models.py"],'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["verify_signature", "_evict_expired_nonces", "NodeRegistration"],
                touched_paths=["hub/auth.py", "hub/db.py", "hub/models.py", "hub/requirements.txt"],
                touched_tests=[],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertIn("Touched paths reviewed: `hub/auth.py`, `hub/db.py`, `hub/models.py`", rendered)
        self.assertIn("Risk areas checked:", rendered)
        self.assertIn("Checks performed:", rendered)
        self.assertIn("Observation:", rendered)
        self.assertNotIn("Why no finding:", rendered)
        self.assertIn("Changed identifiers verified: `verify_signature`, `_evict_expired_nonces`, `NodeRegistration`", rendered)

    def test_structured_review_rewrites_generic_no_finding_text_to_grounded_anchors(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"The patch looks good overall.",'
                '"observation":"The change is well-structured.",'
                '"why_no_finding":"No issues found after review.",'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["verify_signature", "_evict_expired_nonces"],
                touched_paths=["hub/auth.py", "hub/db.py"],
                touched_tests=["tests/test_hub_api.py"],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertIn("Touched paths reviewed: `hub/auth.py`, `hub/db.py`", rendered)
        self.assertIn("Tests reviewed: `tests/test_hub_api.py`", rendered)
        self.assertIn("Changed identifiers verified: `verify_signature`, `_evict_expired_nonces`", rendered)
        self.assertIn("The patch looks good overall.", rendered)
        self.assertIn("Observation: The change is well-structured.", rendered)
        self.assertNotIn("Why no finding:", rendered)

    def test_structured_review_rewrites_partial_diff_caveat_to_grounded_observation(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"The retry update stays narrowly scoped.",'
                '"observation":"The test bodies are not fully shown in the diff.",'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["_suppression_reason_allows_retry", "_issue_retry_task"],
                touched_paths=["bridge/github_to_mep.py", "tests/test_github_bridge.py"],
                touched_tests=["tests/test_github_bridge.py"],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertIn("The retry update stays narrowly scoped.", rendered)
        self.assertIn("Observation:", rendered)
        self.assertNotIn("not fully shown in the diff", rendered)
        self.assertNotIn("verification is limited", rendered)
        self.assertIn("_suppression_reason_allows_retry", rendered)
        self.assertIn("_issue_retry_task", rendered)

    def test_is_weak_review_text_matches_partial_diff_caveat_with_trailing_colon(self):
        self.assertTrue(
            review_patterns.has_partial_diff_caveat("Observation: Partial diff: verification is limited.")
        )
        self.assertTrue(
            mep_runtime._is_weak_review_text(  # noqa: SLF001
                "Observation: Partial diff: verification is limited."
            )
        )

    def test_has_partial_diff_caveat_falls_back_when_shared_import_is_unavailable(self):
        with patch.object(mep_runtime, "_shared_has_partial_diff_caveat", None):
            self.assertTrue(mep_runtime.has_partial_diff_caveat("The test bodies are not fully shown in the diff."))

    def test_finalize_model_reply_drops_trailing_partial_word(self):
        rendered = mep_runtime._finalize_model_reply(  # noqa: SLF001
            (
                "## Review Summary\n\n"
                "The retry update stays grounded and test-aware; "
                "verification remains aligned with the diff and avoids dangling caveats."
            ),
            max_chars=95,
        )

        self.assertTrue(rendered.endswith("."))
        self.assertNotIn("veri.", rendered)
        self.assertNotIn("dangli.", rendered)

    def test_structured_review_synthesizes_grounded_summary_from_non_json_reply(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            "The replay-protection and dependency-pin changes look low-risk after review.",
            max_chars=1000,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["verify_signature", "_evict_expired_nonces"],
                touched_paths=["hub/auth.py", "hub/db.py", "hub/models.py", "hub/requirements.txt"],
                touched_tests=[],
            ),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertIn("Touched paths reviewed: `hub/auth.py`, `hub/db.py`, `hub/models.py`, `hub/requirements.txt`", rendered)
        self.assertIn("Risk areas checked:", rendered)
        self.assertIn("Checks performed:", rendered)
        self.assertIn("Observation:", rendered)
        self.assertNotIn("Why no finding:", rendered)
        self.assertIn("Changed identifiers verified: `verify_signature`, `_evict_expired_nonces`", rendered)

    def test_structured_review_drops_findings_without_allowed_changed_identifiers(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the bridge diff.","observation":"The coalesce path still needs a concrete proof point.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["imagined_guard"],'
                '"findings":[{"file":"bridge/github_to_mep.py","issue":"Preserve coalesced targets",'
                '"rationale":"Otherwise the second mention can overwrite the first target during the coalesce window."}],'
                '"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertNotIn("## Review Findings", rendered)
        self.assertNotIn("Preserve coalesced targets", rendered)
        self.assertIn("verified changed identifiers `_record_pending_task_poll_failure`, `last_poll_status`", rendered)

    def test_structured_review_rewrites_summary_and_observation_with_unallowed_identifiers(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"`_record_pending_task_poll_failure` still looks safe, but `imagined_guard` now drives the retry path.",'
                '"observation":"`last_poll_status` is real, but `imagined_guard` is the branch to watch.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["_record_pending_task_poll_failure","last_poll_status"],'
                '"approval_recommendation":"comment"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertNotIn("imagined_guard", rendered)
        self.assertIn("Touched paths reviewed: `bridge/github_to_mep.py`", rendered)
        self.assertIn("Changed identifiers verified: `_record_pending_task_poll_failure`, `last_poll_status`", rendered)

    def test_structured_review_drops_findings_with_mixed_real_and_fake_identifiers(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the bridge diff.","observation":"The changed path stays narrow.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["_record_pending_task_poll_failure","last_poll_status"],'
                '"findings":[{"file":"bridge/github_to_mep.py","issue":"Mixed identifier claim in `_record_pending_task_poll_failure`",'
                '"rationale":"The retry path now depends on `imagined_guard`, so the second status update can be lost."}],'
                '"approval_recommendation":"comment"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertNotIn("## Review Findings", rendered)
        self.assertNotIn("imagined_guard", rendered)
        self.assertNotIn("Mixed identifier claim", rendered)

    def test_structured_review_drops_findings_with_unsupported_code_snippets(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the runtime diff.","observation":"The truncation helper changed in a narrow way.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["_record_pending_task_poll_failure","last_poll_status"],'
                '"findings":[{"file":"bridge/github_to_mep.py","issue":"False fallback claim in `_record_pending_task_poll_failure`",'
                '"rationale":"The branch now returns `patch_info.get(\'changed_identifiers\', [])`, which leaves stale identifiers behind."}],'
                '"approval_recommendation":"comment"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertNotIn("## Review Findings", rendered)
        self.assertNotIn("patch_info.get('changed_identifiers', [])", rendered)
        self.assertNotIn("False fallback claim", rendered)

    def test_structured_review_drops_malformed_checks_section_entries(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the bridge diff.","observation":"The changed path stays narrow.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["_record_pending_task_poll_failure","last_poll_status"],'
                '"checks_performed":["verified `_record_pending_task_poll_failure` updates `last_poll_status safely ha"],'
                '"findings":[{"file":"bridge/github_to_mep.py","issue":"Guard remains scoped to `_record_pending_task_poll_failure`",'
                '"rationale":"The diff still keeps the metrics update tied to the same helper."}],'
                '"approval_recommendation":"comment"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("## Review Findings", rendered)
        self.assertNotIn("Checks performed:", rendered)
        self.assertNotIn("safely ha", rendered)

    def test_structured_review_drops_non_identifier_verified_identifiers(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the bridge diff.","observation":"The changed path stays narrow.",'
                '"touched_paths":["bridge/github_to_mep.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py"],'
                '"verified_identifiers":["[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+","last_poll_status"],'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(
                changed_identifiers=["_record_pending_task_poll_failure", "last_poll_status"]
            ),
        )

        self.assertIn("Changed identifiers verified: `last_poll_status`", rendered)
        self.assertNotIn("[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+", rendered)

    def test_structured_review_drops_overlong_verified_identifiers_instead_of_clipping(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the runtime diff.","observation":"The changed path stays narrow.",'
                '"touched_paths":["node/mep_runtime.py"],'
                '"tests_reviewed":["tests/test_node_runtime.py"],'
                '"verified_identifiers":["test_structured_approval_review_replaces_behavior_overstatement_with_grounded_summary",'
                '"_trim_dangling_review_tail"],'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1200,
            task_data=self._bridge_review_task_data(
                changed_identifiers=[
                    "test_structured_approval_review_replaces_behavior_overstatement_with_grounded_summary",
                    "_trim_dangling_review_tail",
                ],
                touched_paths=["node/mep_runtime.py", "tests/test_node_runtime.py"],
                touched_tests=["tests/test_node_runtime.py"],
            ),
        )

        self.assertIn("Changed identifiers verified: `_trim_dangling_review_tail`", rendered)
        self.assertNotIn(
            "test_structured_approval_review_replaces_behavior_overstatement_with_grounded_summary",
            rendered,
        )
        self.assertNotIn("test_structured_approval_review_replaces_behavior_overstatement_with_gr", rendered)

    def test_default_structured_review_drops_overlong_changed_identifiers(self):
        rendered = mep_runtime._render_default_structured_review(  # noqa: SLF001
            task_data=self._bridge_review_task_data(
                changed_identifiers=[
                    "test_structured_approval_review_replaces_behavior_overstatement_with_grounded_summary",
                    "_trim_dangling_review_tail",
                ],
                touched_paths=["node/mep_runtime.py", "tests/test_node_runtime.py"],
                touched_tests=["tests/test_node_runtime.py"],
            ),
            max_chars=1200,
        )

        self.assertIn("_trim_dangling_review_tail", rendered)
        self.assertNotIn(
            "test_structured_approval_review_replaces_behavior_overstatement_with_grounded_summary",
            rendered,
        )
        self.assertNotIn("test_structured_approval_review_replaces_behavior_overstatement_with_gr", rendered)

    def test_structured_review_drops_changed_identifiers_not_visible_in_patch_excerpt(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the runtime diff.","observation":"The changed path stays narrow.",'
                '"touched_paths":["bridge/github_to_mep.py","node/mep_runtime.py","tests/test_github_bridge.py"],'
                '"tests_reviewed":["tests/test_github_bridge.py","tests/test_node_runtime.py"],'
                '"verified_identifiers":["_trim_dangling_review_tail","test_status_callback_publishes_reviewed_blocker_when_approve_checks_are_pending"],'
                '"findings":[],"approval_recommendation":"comment"}'
            ),
            max_chars=1400,
            task_data=self._bridge_review_task_data(
                changed_identifiers=[
                    "_trim_dangling_review_tail",
                    "test_status_callback_publishes_reviewed_blocker_when_approve_checks_are_pending",
                ],
                touched_paths=["bridge/github_to_mep.py", "node/mep_runtime.py", "tests/test_github_bridge.py"],
                touched_tests=["tests/test_github_bridge.py", "tests/test_node_runtime.py"],
                changed_files=[
                    {
                        "filename": "node/mep_runtime.py",
                        "patch_excerpt": "@@ -697,0 +698,14 @@\n+def _trim_dangling_review_tail(text: str, *, clipped_from_longer: bool) -> str:\n+    return cleaned\n",
                    },
                    {
                        "filename": "tests/test_github_bridge.py",
                        "patch_excerpt": "@@ -3200,0 +3210,10 @@\n+def test_other_changed_case(self):\n+    assert sanitized\n",
                    },
                ],
            ),
        )

        self.assertIn("Changed identifiers verified: `_trim_dangling_review_tail`", rendered)
        self.assertNotIn(
            "test_status_callback_publishes_reviewed_blocker_when_approve_checks_are_pending",
            rendered,
        )

    def test_structured_review_drops_findings_for_untouched_files(self):
        rendered = mep_runtime._render_structured_review_with_task_data(  # noqa: SLF001
            (
                '{"summary":"Checked the bridge diff.","observation":"The real change stays scoped to the bridge entrypoint.",'
                '"touched_paths":["bridge/other_file.py"],'
                '"tests_reviewed":["tests/test_other_file.py"],'
                '"verified_identifiers":["_record_pending_task_poll_failure"],'
                '"findings":[{"file":"bridge/not_touched.py","issue":"Unexpected side effect",'
                '"rationale":"This claim points at a file that is not part of the supplied diff."}],'
                '"approval_recommendation":"comment"}'
            ),
            max_chars=1000,
            task_data=self._bridge_review_task_data(),
        )

        self.assertIn("## Review Summary", rendered)
        self.assertNotIn("## Review Findings", rendered)
        self.assertNotIn("Unexpected side effect", rendered)
        self.assertIn("Touched paths reviewed: `bridge/github_to_mep.py`", rendered)
        self.assertIn("Tests reviewed: `tests/test_github_bridge.py`", rendered)

    def test_extract_review_candidates_deduplicates_and_cleans_items(self):
        candidates = mep_runtime._extract_review_candidates(  # noqa: SLF001
            (
                '{"risk_candidates":['
                '{"file":"bridge/github_to_mep.py","category":"automation/writeback safety","priority":"high","claim":"Coalesce window can overwrite the first target","reason":"Multiple writes share the same metadata bucket","evidence":["preserve_coalesced_targets"]},'
                '{"file":"bridge/github_to_mep.py","category":"automation/writeback safety","priority":"low","claim":"Coalesce window can overwrite the first target","reason":"duplicate","evidence":["preserve_coalesced_targets"]},'
                '{"file":"","claim":"  ","reason":"empty"}'
                ']}'
            )
        )

        self.assertEqual(
            candidates,
            [
                {
                    "file": "bridge/github_to_mep.py",
                    "category": "automation/writeback safety",
                    "priority": "high",
                    "claim": "Coalesce window can overwrite the first target.",
                    "reason": "Multiple writes share the same metadata bucket.",
                    "evidence": ["preserve_coalesced_targets"],
                }
            ],
        )

    def test_extract_review_candidates_ranks_high_priority_first(self):
        candidates = mep_runtime._extract_review_candidates(  # noqa: SLF001
            (
                '{"risk_candidates":['
                '{"file":"bridge/github_to_mep.py","category":"test gap","priority":"low","claim":"Missing edge-case coverage","reason":"No regression test covers the fallback branch"},'
                '{"file":"bridge/github_to_mep.py","category":"security/trust-boundary","priority":"high","claim":"Approval path can publish before the stronger guard runs","reason":"The changed branch still accepts the optimistic action first","evidence":["_approval_quality_failure"]}'
                ']}'
            )
        )

        self.assertEqual(candidates[0]["priority"], "high")
        self.assertEqual(candidates[0]["category"], "security/trust-boundary")
        self.assertEqual(candidates[0]["evidence"], ["_approval_quality_failure"])


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

        adapter_mock.assert_called_once()
        self.assertEqual(adapter_mock.call_args.args[0], "Use only this instruction text.")
        adapter_task_data = adapter_mock.call_args.args[1]
        self.assertEqual(adapter_task_data["payload"], task_data["payload"])
        self.assertEqual(adapter_task_data["task"]["instructions"], "Use only this instruction text.")
        self.assertEqual(adapter_task_data["intent"]["type"], "chat.request")
        complete_mock.assert_called_once_with("task_interbot", "reply")

    def test_process_task_downgrades_plaintext_approval_to_reviewed_bridge_status(self):
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
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["action"], "reviewed")
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["detail"], "Looks good to me.")
        self.assertEqual(request_mock.call_args.kwargs["headers"]["Authorization"], "Bearer status-token")

    def test_process_task_keeps_grounded_approval_as_approved_bridge_status(self):
        node = _runtime_node()
        detail = (
            "## Review Summary\n\n"
            "Verified the approval gate stays low-risk.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )
        task_data = TestRuntimeReviewPrompts._bridge_review_task_data(  # noqa: SLF001
            intent_type="code.review.approve",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
        )
        with (
            patch.object(node.adapter, "generate_reply", return_value=detail),
            patch.object(node, "complete") as complete_mock,
            patch("node.mep_runtime._safe_request", return_value=(200, {}, "")) as request_mock,
        ):
            asyncio.run(node.process_task(task_data))

        complete_mock.assert_called_once_with("task_bridge_review", detail)
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["action"], "approved")

    def test_process_task_downgrades_grounded_approval_when_checks_pending(self):
        node = _runtime_node()
        detail = (
            "## Review Summary\n\n"
            "Verified the approval gate stays low-risk.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )
        task_data = TestRuntimeReviewPrompts._bridge_review_task_data(  # noqa: SLF001
            intent_type="code.review.approve",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
            ci_checks={"has_checks": True, "state": "pending", "all_green": False},
        )
        with (
            patch.object(node.adapter, "generate_reply", return_value=detail),
            patch.object(node, "complete") as complete_mock,
            patch("node.mep_runtime._safe_request", return_value=(200, {}, "")) as request_mock,
        ):
            asyncio.run(node.process_task(task_data))

        complete_mock.assert_called_once_with("task_bridge_review", detail)
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.kwargs["json_body"]["action"], "reviewed")

    def test_review_request_only_upgrades_to_approved_when_checks_green(self):
        detail = (
            "## Review Summary\n\n"
            "Verified the approval gate stays low-risk.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )
        pending_task_data = TestRuntimeReviewPrompts._bridge_review_task_data(  # noqa: SLF001
            intent_type="code.review.request",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
            ci_checks={"has_checks": True, "state": "pending", "all_green": False},
        )
        green_task_data = TestRuntimeReviewPrompts._bridge_review_task_data(  # noqa: SLF001
            intent_type="code.review.request",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
            ci_checks={"has_checks": True, "state": "green", "all_green": True},
        )

        pending_action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            json.loads(pending_task_data["payload"]),
            detail=detail,
            task_data=pending_task_data,
        )
        green_action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            json.loads(green_task_data["payload"]),
            detail=detail,
            task_data=green_task_data,
        )

        self.assertEqual(pending_action, "reviewed")
        self.assertEqual(green_action, "approved")

    def test_review_request_does_not_treat_truthy_non_bool_checks_as_green(self):
        detail = (
            "## Review Summary\n\n"
            "Verified the approval gate stays low-risk.\n\n"
            "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
            "Tests reviewed: `tests/test_github_bridge.py`\n\n"
            "Risk areas checked: approval gating, changed-line anchoring\n\n"
            "Checks performed: traced approval suppression branches, compared changed identifiers against the diff\n\n"
            "Changed identifiers verified: `_score_review_quality`, `_approval_quality_failure`"
        )
        task_data = TestRuntimeReviewPrompts._bridge_review_task_data(  # noqa: SLF001
            intent_type="code.review.request",
            changed_identifiers=["_score_review_quality", "_approval_quality_failure"],
            ci_checks={"has_checks": True, "state": "green", "all_green": "true"},
        )

        action = mep_runtime.RuntimeNode._bridge_status_action(  # noqa: SLF001
            json.loads(task_data["payload"]),
            detail=detail,
            task_data=task_data,
        )

        self.assertEqual(action, "reviewed")

    def test_process_task_reports_failed_status_when_adapter_errors_on_review(self):
        node = _runtime_node()
        task_data = {
            "id": "task_bridge_error",
            "bounty": 0.0,
            "payload": json.dumps(
                {
                    "spec_version": "mep.interbot.v1",
                    "message_id": "msg-bridge-error",
                    "trace_id": "trace-bridge-error",
                    "source": {"node_id": "node_bridge"},
                    "target": {"node_id": node.node_id},
                    "conversation": {"context_id": "ctx-bridge-error"},
                    "intent": {"type": "code.review.approve", "priority": "high"},
                    "task": {
                        "instructions": "Approve this PR if it looks good.",
                        "inputs": {
                            "bridge_metadata": {
                                "source_type": "github",
                                "bridge_id": "br-err-1",
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
        error_reply = '[DeepSeek] API error 402: {"error":{"message":"Insufficient Balance"}}'
        with (
            patch.object(node.adapter, "generate_reply", return_value=error_reply),
            patch.object(node, "complete") as complete_mock,
            patch("node.mep_runtime._safe_request", return_value=(200, {}, "")) as request_mock,
        ):
            asyncio.run(node.process_task(task_data))

        complete_mock.assert_called_once()
        request_mock.assert_called_once()
        body = request_mock.call_args.kwargs["json_body"]
        self.assertEqual(body["status"], "failed")
        self.assertNotIn("action", body)

    def test_process_task_appends_synced_workspace_context_to_review_input(self):
        node = _runtime_node()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "bridge"), exist_ok=True)
            with open(os.path.join(tmpdir, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "def untouched_helper():\n    return False\n\n"
                    "def live_sync_context():\n    state_value = True\n    return state_value\n"
                )
            os.makedirs(os.path.join(tmpdir, "tests"), exist_ok=True)
            with open(os.path.join(tmpdir, "tests", "test_bridge_sync.py"), "w", encoding="utf-8") as handle:
                handle.write("def test_live_sync_context():\n    assert True\n")

            task_data = TestRuntimeReviewPrompts._bridge_review_task_data()
            payload = json.loads(task_data["payload"])
            payload["task"]["inputs"]["github"].update(
                {
                    "repo_clone_url": "https://github.com/example/repo.git",
                    "head_sha": "abc12345",
                    "head_ref": "feature/test",
                    "touched_tests": ["tests/test_bridge_sync.py"],
                    "risk_pack": {
                        "changed_identifiers": ["live_sync_context", "state_value"],
                        "touched_non_test_paths": ["bridge/github_to_mep.py"],
                    },
                }
            )
            task_data["payload"] = json.dumps(payload)

            with (
                patch.object(node.workspace, "sync_pr_workspace", return_value=(True, tmpdir)) as sync_mock,
                patch.object(node.adapter, "generate_reply", return_value="reply") as adapter_mock,
                patch.object(node, "complete") as complete_mock,
            ):
                asyncio.run(node.process_task(task_data))

        sync_mock.assert_called_once_with(
            "https://github.com/example/repo.git",
            "abc12345",
            "feature/test",
            bridge_id="trace-bridge-review",
        )
        instructions, adapter_task_data = adapter_mock.call_args.args
        self.assertIn("Additional local workspace context:", instructions)
        self.assertIn("Local workspace path:", instructions)
        self.assertIn("Hunk-centered local context pack", instructions)
        self.assertIn("live_sync_context", instructions)
        self.assertIn("test_live_sync_context", instructions)
        self.assertEqual(
            adapter_task_data["task"]["inputs"]["github"]["local_workspace_path"],
            tmpdir,
        )
        complete_mock.assert_called_once_with("task_bridge_review", "reply")

    def test_process_task_appends_synced_repo_audit_context_and_inventory(self):
        node = _runtime_node()
        task_data = {
            "id": "task_repo_audit",
            "bounty": 0.0,
            "payload": "Run a repo audit for github.com/WUAIBING/MEP.",
            "intent": {"type": "repo_audit.request"},
            "task": {
                "instructions": "Run a repo audit for github.com/WUAIBING/MEP.",
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "audit_type": "full_repo_audit",
                    }
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(node.workspace, "sync_repo_audit_workspace", return_value=(True, tmpdir)) as sync_mock,
                patch.object(
                    node.workspace,
                    "build_repo_audit_context",
                    return_value=("Local workspace path: tmpdir\n- README.md", ["README.md", "node/mep_runtime.py"]),
                ) as context_mock,
                patch.object(node.adapter, "generate_reply", return_value="repo audit reply") as adapter_mock,
                patch.object(node, "complete") as complete_mock,
            ):
                asyncio.run(node.process_task(task_data))

        sync_mock.assert_called_once_with("github.com/WUAIBING/MEP", "main")
        context_mock.assert_called_once_with(tmpdir)
        instructions, adapter_task_data = adapter_mock.call_args.args
        self.assertIn("Authoritative local repo audit context:", instructions)
        self.assertIn("README.md", instructions)
        repo_audit_inputs = adapter_task_data["task"]["inputs"]["repo_audit"]
        self.assertEqual(repo_audit_inputs["local_workspace_path"], tmpdir)
        self.assertEqual(repo_audit_inputs["inventory_paths"], ["README.md", "node/mep_runtime.py"])
        complete_mock.assert_called_once_with("task_repo_audit", "repo audit reply")

    def test_process_task_fails_closed_when_repo_audit_workspace_sync_fails(self):
        node = _runtime_node()
        task_data = {
            "id": "task_repo_audit_fail",
            "bounty": 0.0,
            "payload": "Run a repo audit for github.com/WUAIBING/MEP.",
            "intent": {"type": "repo_audit.request"},
            "task": {
                "instructions": "Run a repo audit for github.com/WUAIBING/MEP.",
                "inputs": {"repo_audit": {"repo_url": "github.com/WUAIBING/MEP", "ref": "main"}},
            },
        }

        with (
            patch.object(node.workspace, "sync_repo_audit_workspace", return_value=(False, "fetch failed")) as sync_mock,
            patch.object(node.adapter, "generate_reply") as adapter_mock,
            patch.object(node, "complete") as complete_mock,
        ):
            asyncio.run(node.process_task(task_data))

        sync_mock.assert_called_once_with("github.com/WUAIBING/MEP", "main")
        adapter_mock.assert_not_called()
        complete_mock.assert_called_once_with("task_repo_audit_fail", "[repo audit] workspace sync failed: fetch failed")

    def test_process_task_fails_closed_when_repo_audit_contract_lacks_inputs(self):
        node = _runtime_node()
        task_data = {
            "id": "task_repo_audit_missing_inputs",
            "bounty": 0.0,
            "payload": "Run a repo audit for github.com/WUAIBING/MEP.",
            "model_requirement": "repo_audit",
            "task": {
                "instructions": "Run a repo audit for github.com/WUAIBING/MEP.",
                "title": "Repo audit: github.com/WUAIBING/MEP",
                "expected_output": {"result_type": "repo_audit_result"},
            },
        }

        with (
            patch.object(node.workspace, "sync_repo_audit_workspace") as sync_mock,
            patch.object(node.adapter, "generate_reply") as adapter_mock,
            patch.object(node, "complete") as complete_mock,
        ):
            asyncio.run(node.process_task(task_data))

        sync_mock.assert_not_called()
        adapter_mock.assert_not_called()
        complete_mock.assert_called_once_with(
            "task_repo_audit_missing_inputs",
            "[repo audit] missing structured repo_audit inputs; refusing ungrounded audit",
        )

    def test_process_task_fails_closed_when_repo_audit_context_has_no_inventory(self):
        node = _runtime_node()
        task_data = {
            "id": "task_repo_audit_no_inventory",
            "bounty": 0.0,
            "payload": "Run a repo audit for github.com/WUAIBING/MEP.",
            "intent": {"type": "repo_audit.request"},
            "task": {
                "instructions": "Run a repo audit for github.com/WUAIBING/MEP.",
                "inputs": {"repo_audit": {"repo_url": "github.com/WUAIBING/MEP", "ref": "main"}},
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(node.workspace, "sync_repo_audit_workspace", return_value=(True, tmpdir)) as sync_mock,
                patch.object(node.workspace, "build_repo_audit_context", return_value=("Local workspace path: tmpdir", [])) as context_mock,
                patch.object(node.adapter, "generate_reply") as adapter_mock,
                patch.object(node, "complete") as complete_mock,
            ):
                asyncio.run(node.process_task(task_data))

        sync_mock.assert_called_once_with("github.com/WUAIBING/MEP", "main")
        context_mock.assert_called_once_with(tmpdir)
        adapter_mock.assert_not_called()
        complete_mock.assert_called_once_with(
            "task_repo_audit_no_inventory",
            "[repo audit] workspace context missing: tracked-file inventory unavailable",
        )

    def test_repo_audit_prompt_recovers_when_intent_is_missing_but_contract_survives(self):
        task_data = {
            "model_requirement": "repo_audit",
            "task": {
                "title": "Repo audit: github.com/WUAIBING/MEP",
                "expected_output": {"result_type": "repo_audit_result"},
                "inputs": {"repo_audit": {"repo_url": "github.com/WUAIBING/MEP"}},
            },
        }

        self.assertTrue(mep_runtime._task_requires_repo_audit_contract(task_data))  # noqa: SLF001
        self.assertTrue(mep_runtime._task_requires_repo_audit_prompt(task_data))  # noqa: SLF001

    def test_process_task_skips_verification_for_untrusted_contributor_by_default(self):
        node = _runtime_node()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "bridge"), exist_ok=True)
            with open(os.path.join(tmpdir, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write("def live_sync_context():\n    return True\n")

            task_data = TestRuntimeReviewPrompts._bridge_review_task_data()
            payload = json.loads(task_data["payload"])
            payload["task"]["inputs"]["github"].update(
                {
                    "repo_clone_url": "https://github.com/example/repo.git",
                    "head_sha": "abc12345",
                    "head_ref": "feature/test",
                    "author_association": "CONTRIBUTOR",
                }
            )
            task_data["payload"] = json.dumps(payload)

            with (
                patch.object(node.workspace, "sync_pr_workspace", return_value=(True, tmpdir)),
                patch.object(node.workspace, "build_review_context", return_value="Local workspace path: tmpdir"),
                patch.object(node.workspace, "build_verification_report", return_value="should not run") as verify_mock,
                patch.object(node.adapter, "generate_reply", return_value="reply") as adapter_mock,
                patch.object(node, "complete"),
            ):
                asyncio.run(node.process_task(task_data))

        self.assertFalse(verify_mock.called)
        instructions = adapter_mock.call_args.args[0]
        self.assertIn("Automated verification checks were skipped", instructions)
        self.assertIn("CONTRIBUTOR", instructions)

    def test_process_task_allows_external_verification_when_explicitly_enabled(self):
        node = _runtime_node()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {"MEP_REVIEW_ALLOW_EXTERNAL_CHECKS": "1"}),
        ):
            os.makedirs(os.path.join(tmpdir, "bridge"), exist_ok=True)
            with open(os.path.join(tmpdir, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write("def live_sync_context():\n    return True\n")

            task_data = TestRuntimeReviewPrompts._bridge_review_task_data()
            payload = json.loads(task_data["payload"])
            payload["task"]["inputs"]["github"].update(
                {
                    "repo_clone_url": "https://github.com/example/repo.git",
                    "head_sha": "abc12345",
                    "head_ref": "feature/test",
                    "author_association": "CONTRIBUTOR",
                }
            )
            task_data["payload"] = json.dumps(payload)

            with (
                patch.object(node.workspace, "sync_pr_workspace", return_value=(True, tmpdir)),
                patch.object(node.workspace, "build_review_context", return_value="Local workspace path: tmpdir"),
                patch.object(node.workspace, "build_verification_report", return_value="Automated verification run") as verify_mock,
                patch.object(node.adapter, "generate_reply", return_value="reply") as adapter_mock,
                patch.object(node, "complete"),
            ):
                asyncio.run(node.process_task(task_data))

        self.assertTrue(verify_mock.called)
        instructions = adapter_mock.call_args.args[0]
        self.assertIn("Automated verification run", instructions)
        self.assertNotIn("Automated verification checks were skipped", instructions)

    def test_process_task_includes_workspace_tool_evidence_for_pr_reviews(self):
        node = _runtime_node()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "bridge"), exist_ok=True)
            with open(os.path.join(tmpdir, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write("def live_sync_context():\n    return True\n")

            task_data = TestRuntimeReviewPrompts._bridge_review_task_data()
            payload = json.loads(task_data["payload"])
            payload["task"]["inputs"]["github"].update(
                {
                    "repo_clone_url": "https://github.com/example/repo.git",
                    "head_sha": "abc12345",
                    "head_ref": "feature/test",
                }
            )
            task_data["payload"] = json.dumps(payload)

            with (
                patch.object(
                    node,
                    "_build_github_context",
                    return_value=(
                        "GitHub context:\n- Scope: `WUAIBING/MEP#246`\n- Title: Tighten bridge review grounding",
                        {"scope": "WUAIBING/MEP#246", "head_ref": "feature/test", "source": "task_inputs"},
                    ),
                ),
                patch.object(node.workspace, "sync_pr_workspace", return_value=(True, tmpdir)),
                patch.object(node.workspace, "build_review_context", return_value="Local workspace path: tmpdir"),
                patch.object(node.workspace, "build_workspace_search_context", return_value="workspace_search hits from the checked-out workspace:\n\n### live_sync_context"),
                patch.object(node.workspace, "build_workspace_git_context", return_value="workspace_git snapshot from the checked-out workspace:\n\n- HEAD commit: abc12345"),
                patch.object(node.workspace, "build_verification_report", return_value="") as verify_mock,
                patch.object(node.adapter, "generate_reply", return_value="reply") as adapter_mock,
                patch.object(node, "complete"),
            ):
                asyncio.run(node.process_task(task_data))

        self.assertTrue(verify_mock.called)
        instructions, adapter_task_data = adapter_mock.call_args.args
        self.assertIn("GitHub context:", instructions)
        self.assertIn("Additional workspace_search evidence:", instructions)
        self.assertIn("Additional workspace_git evidence:", instructions)
        self.assertIn("Deep review escalation is active for this task.", instructions)
        self.assertIn("Runtime tool evidence bundle:", instructions)
        runtime_tool_bundle = adapter_task_data["task"]["inputs"]["runtime_tool_bundle"]
        self.assertEqual(runtime_tool_bundle["contract_version"], "mep.runtime_tools.v1")
        self.assertEqual(runtime_tool_bundle["task_mode"], "review")
        self.assertEqual(
            [item["tool"] for item in runtime_tool_bundle["runs"]],
            ["github_context", "workspace_read", "workspace_search", "workspace_git", "targeted_verify"],
        )

    def test_process_task_reports_tools_called_from_runtime_tool_runs(self):
        node = _runtime_node()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "bridge"), exist_ok=True)
            with open(os.path.join(tmpdir, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write("def live_sync_context():\n    return True\n")

            task_data = TestRuntimeReviewPrompts._bridge_review_task_data()
            payload = json.loads(task_data["payload"])
            payload["task"]["inputs"]["github"].update(
                {
                    "repo_clone_url": "https://github.com/example/repo.git",
                    "head_sha": "abc12345",
                    "head_ref": "feature/test",
                }
            )
            task_data["payload"] = json.dumps(payload)

            node.adapter.last_review_metrics = {
                "model": "test-model",
                "tokens_in": 100,
                "tokens_out": 50,
                "tools_called": 0,
                "review_passes": 2,
                "token_source": "provider",
            }

            with (
                patch.object(
                    node,
                    "_build_github_context",
                    return_value=(
                        "GitHub context:\n- Scope: `WUAIBING/MEP#246`",
                        {"scope": "WUAIBING/MEP#246", "head_ref": "feature/test", "source": "task_inputs"},
                    ),
                ),
                patch.object(node.workspace, "sync_pr_workspace", return_value=(True, tmpdir)),
                patch.object(node.workspace, "build_review_context", return_value="Local workspace path: tmpdir"),
                patch.object(node.workspace, "build_workspace_search_context", return_value="workspace_search hits from the checked-out workspace:\n\n### live_sync_context"),
                patch.object(node.workspace, "build_workspace_git_context", return_value="workspace_git snapshot from the checked-out workspace:\n\n- HEAD commit: abc12345"),
                patch.object(node.workspace, "build_verification_report", return_value=""),
                patch.object(node.adapter, "generate_reply", return_value="reply"),
                patch.object(node, "complete"),
                patch.object(node, "_report_bridge_status") as report_mock,
            ):
                asyncio.run(node.process_task(task_data))

        self.assertTrue(report_mock.called)
        reported_metrics = report_mock.call_args.kwargs.get("review_metrics")
        self.assertIsInstance(reported_metrics, dict)
        # github_context, workspace_read, workspace_search, workspace_git succeeded;
        # targeted_verify returned empty (no run recorded).
        self.assertGreaterEqual(reported_metrics["tools_called"], 4)
        self.assertEqual(reported_metrics["tokens_in"], 100)
        # The adapter's own metrics dict must not be mutated.
        self.assertEqual(node.adapter.last_review_metrics["tools_called"], 0)

    def test_build_github_context_prefers_api_fields_when_available(self):
        node = _runtime_node()
        github_inputs = {
            "repo_full_name": "WUAIBING/MEP",
            "entity_type": "pr",
            "number": 246,
            "review_mode": "discovery_review",
            "ci_checks": {"has_checks": True, "state": "success", "all_green": True},
        }
        with patch.object(
            node,
            "_fetch_github_pr_context",
            return_value={
                "title": "Investigate trust boundary drift",
                "body": "Focus on auth callers and approval routing.",
                "user": {"login": "alice"},
                "head": {"ref": "feature/runtime-tools"},
                "base": {"ref": "main"},
                "labels": [{"name": "review-runtime"}],
            },
        ):
            rendered, payload = node._build_github_context(github_inputs)

        self.assertIn("GitHub context:", rendered)
        self.assertIn("Investigate trust boundary drift", rendered)
        self.assertIn("`feature/runtime-tools` -> `main`", rendered)
        self.assertEqual(payload["source"], "github_api")

    def test_process_task_attaches_runtime_tool_bundle_for_repo_audit(self):
        node = _runtime_node()
        task_data = {
            "id": "task_repo_audit",
            "bounty": 0.0,
            "payload": "Run a repo audit for github.com/WUAIBING/MEP.",
            "intent": {"type": "repo_audit.request"},
            "task": {
                "instructions": "Run a repo audit for github.com/WUAIBING/MEP.",
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "audit_type": "full_repo_audit",
                    }
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(node.workspace, "sync_repo_audit_workspace", return_value=(True, tmpdir)),
                patch.object(
                    node.workspace,
                    "build_repo_audit_context",
                    return_value=("Local workspace path: tmpdir\n- README.md", ["README.md", "node/mep_runtime.py"]),
                ),
                patch.object(
                    node.workspace,
                    "build_workspace_search_context",
                    return_value="workspace_search hits from the checked-out workspace:\n\n### mep_runtime",
                ),
                patch.object(
                    node.workspace,
                    "build_workspace_git_context",
                    return_value="workspace_git snapshot from the checked-out workspace:\n\n- HEAD commit: abc12345",
                ),
                patch.object(node.adapter, "generate_reply", return_value="repo audit reply") as adapter_mock,
                patch.object(node, "complete"),
            ):
                asyncio.run(node.process_task(task_data))

        instructions, adapter_task_data = adapter_mock.call_args.args
        self.assertIn("Additional repo workspace_search evidence:", instructions)
        self.assertIn("Additional repo workspace_git evidence:", instructions)
        self.assertIn("Runtime tool evidence bundle:", instructions)
        runtime_tool_bundle = adapter_task_data["task"]["inputs"]["runtime_tool_bundle"]
        self.assertEqual(runtime_tool_bundle["contract_version"], "mep.runtime_tools.v1")
        self.assertEqual(runtime_tool_bundle["task_mode"], "repo_audit")
        self.assertEqual(
            [item["tool"] for item in runtime_tool_bundle["runs"]],
            ["workspace_read", "workspace_search", "workspace_git"],
        )

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
            with patch.dict(os.environ, {"MEP_KEY_DIR": tmpdir}, clear=False):
                MEPIdentity(os.path.join(tmpdir, "alpha.pem"))
                MEPIdentity(os.path.join(tmpdir, "beta.pem"))

                with patch("builtins.print") as print_mock:
                    code = mep_runtime.main(["run"])

            self.assertEqual(code, 2)
            printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
            self.assertIn("multiple local identities found", printed)


class TestAdapterErrorDetection(unittest.TestCase):
    def test_is_adapter_error_detects_error_sentinels(self):
        self.assertTrue(mep_runtime._is_adapter_error("[DeepSeek] API error 402: Insufficient Balance"))  # noqa: SLF001
        self.assertTrue(mep_runtime._is_adapter_error("[DeepSeek] error: connection reset"))  # noqa: SLF001
        self.assertTrue(mep_runtime._is_adapter_error("[AI adapter] tinyllama timed out"))  # noqa: SLF001
        self.assertTrue(mep_runtime._is_adapter_error("[AI adapter] empty response from tinyllama"))  # noqa: SLF001
        self.assertTrue(mep_runtime._is_adapter_error(""))  # noqa: SLF001

    def test_is_adapter_error_allows_real_reviews(self):
        self.assertFalse(  # noqa: SLF001
            mep_runtime._is_adapter_error("## Review Summary\n\nThe change is scoped and tested.")
        )


class TestWorkspaceReviewContext(unittest.TestCase):
    def test_build_review_context_includes_full_file_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bridge"), exist_ok=True)
            body = "\n".join(f"line_{i} = {i}" for i in range(200))
            with open(os.path.join(tmp, "bridge", "big.py"), "w", encoding="utf-8") as handle:
                handle.write(body)

            wm = mep_runtime.WorkspaceManager(tmp)
            ctx = wm.build_review_context(tmp, ["bridge/big.py"])

        self.assertIn("Full contents fallback for changed files", ctx)
        self.assertIn("line_0 = 0", ctx)
        # The tail of a >700 char file must be present: earlier excerpt logic clipped it.
        self.assertIn("line_199 = 199", ctx)

    def test_build_review_context_prioritizes_changed_identifiers_and_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bridge"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
            with open(os.path.join(tmp, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "def untouched_helper():\n    return False\n\n"
                    "def build_review_context():\n    focus_value = 'ready'\n    return focus_value\n"
                )
            with open(os.path.join(tmp, "tests", "test_bridge_review.py"), "w", encoding="utf-8") as handle:
                handle.write("def test_build_review_context():\n    assert True\n")

            wm = mep_runtime.WorkspaceManager(tmp)
            ctx = wm.build_review_context(
                tmp,
                ["bridge/github_to_mep.py", "tests/test_bridge_review.py"],
                touched_tests=["tests/test_bridge_review.py"],
                risk_pack={
                    "changed_identifiers": ["build_review_context", "focus_value"],
                    "touched_non_test_paths": ["bridge/github_to_mep.py"],
                },
            )

        self.assertIn("Hunk-centered local context pack", ctx)
        self.assertIn("build_review_context", ctx)
        self.assertIn("focus_value", ctx)
        self.assertIn("test_build_review_context", ctx)

    def test_build_verification_report_disabled_by_default(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"MEP_REVIEW_RUN_CHECKS": "0"}),
        ):
            wm = mep_runtime.WorkspaceManager(tmp)
            self.assertEqual(
                wm.build_verification_report(tmp, ["node/x.py"], ["tests/test_x.py"]),
                "",
            )

    def test_build_workspace_search_context_uses_python_fallback_when_rg_missing(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("node.mep_runtime.shutil.which", return_value=None),
        ):
            os.makedirs(os.path.join(tmp, "bridge"), exist_ok=True)
            with open(os.path.join(tmp, "bridge", "github_to_mep.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "def build_review_context():\n"
                    "    focus_value = 'ready'\n"
                    "    return focus_value\n"
                )
            wm = mep_runtime.WorkspaceManager(tmp)
            ctx = wm.build_workspace_search_context(
                tmp,
                touched_paths=["bridge/github_to_mep.py"],
                risk_pack={"changed_identifiers": ["build_review_context", "focus_value"]},
            )

        self.assertIn("workspace_search hits from the checked-out workspace", ctx)
        self.assertIn("build_review_context", ctx)
        self.assertIn("focus_value", ctx)

    def test_build_workspace_git_context_reports_head_and_tracked_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            wm = mep_runtime.WorkspaceManager(tmp)

            def fake_run_git(_cwd, args, *, timeout_seconds=60):
                if args == ["rev-parse", "HEAD"]:
                    return 0, "abc12345deadbeef"
                if args == ["status", "--short", "--untracked-files=no"]:
                    return 0, ""
                if args[:2] == ["ls-files", "--"]:
                    return 0, "bridge/github_to_mep.py\n"
                return 0, ""

            with patch.object(wm, "_run_git", side_effect=fake_run_git):
                ctx = wm.build_workspace_git_context(
                    tmp,
                    touched_paths=["bridge/github_to_mep.py"],
                )

        self.assertIn("workspace_git snapshot from the checked-out workspace", ctx)
        self.assertIn("abc12345deadbeef", ctx)
        self.assertIn("Git status: clean", ctx)
        self.assertIn("bridge/github_to_mep.py", ctx)

    def test_build_repo_audit_context_returns_inventory_and_key_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "node"), exist_ok=True)
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as handle:
                handle.write("# MEP\n")
            with open(os.path.join(tmp, "node", "mep_runtime.py"), "w", encoding="utf-8") as handle:
                handle.write("def runtime_main():\n    return 'ok'\n")
            wm = mep_runtime.WorkspaceManager(tmp)
            with patch.object(
                wm,
                "_run_git",
                return_value=(0, "README.md\nnode/mep_runtime.py\n"),
            ):
                ctx, inventory = wm.build_repo_audit_context(tmp)

        self.assertEqual(inventory, ["README.md", "node/mep_runtime.py"])
        self.assertIn("Tracked file inventory", ctx)
        self.assertIn("README.md", ctx)
        self.assertIn("runtime_main", ctx)

    def test_build_workspace_search_context_uses_inventory_terms_for_repo_audit(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("node.mep_runtime.shutil.which", return_value=None),
        ):
            os.makedirs(os.path.join(tmp, "node"), exist_ok=True)
            with open(os.path.join(tmp, "node", "mep_runtime.py"), "w", encoding="utf-8") as handle:
                handle.write("def mep_runtime_main():\n    return 'ok'\n")
            wm = mep_runtime.WorkspaceManager(tmp)
            ctx = wm.build_workspace_search_context(
                tmp,
                inventory_paths=["README.md", "node/mep_runtime.py"],
            )

        self.assertIn("workspace_search hits from the checked-out workspace", ctx)
        self.assertIn("mep_runtime", ctx)
        self.assertIn("mep_runtime_main", ctx)

    def test_sync_repo_audit_workspace_fetches_target_ref_without_all_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            wm = mep_runtime.WorkspaceManager(tmp)
            workspace_path = os.path.join(
                tmp,
                "repo-audit",
                wm._workspace_slug("https://github.com/WUAIBING/MEP.git"),  # noqa: SLF001
            )
            os.makedirs(os.path.join(workspace_path, ".git"), exist_ok=True)
            calls: list[tuple[str, list[str], int]] = []

            def fake_run_git(cwd, args, *, timeout_seconds=60):
                calls.append((cwd, list(args), timeout_seconds))
                if args[:4] == ["fetch", "--no-tags", "origin", "main"]:
                    return 0, "fetched"
                if args[:3] == ["checkout", "--force", "FETCH_HEAD"]:
                    return 0, "checked out"
                return 0, ""

            with patch.object(wm, "_run_git", side_effect=fake_run_git):
                ok, path = wm.sync_repo_audit_workspace("github.com/WUAIBING/MEP", "main")

        self.assertTrue(ok)
        self.assertEqual(path, workspace_path)
        self.assertEqual(calls[0][1], ["fetch", "--no-tags", "origin", "main"])
        self.assertEqual(calls[1][1], ["checkout", "--force", "FETCH_HEAD"])
        self.assertTrue(all("--all" not in args and "--tags" not in args for _cwd, args, _timeout in calls))

    def test_sync_repo_audit_workspace_uses_repo_audit_git_timeout_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            wm = mep_runtime.WorkspaceManager(tmp)
            workspace_path = os.path.join(
                tmp,
                "repo-audit",
                wm._workspace_slug("https://github.com/WUAIBING/MEP.git"),  # noqa: SLF001
            )
            os.makedirs(os.path.join(workspace_path, ".git"), exist_ok=True)
            calls: list[tuple[str, list[str], int]] = []

            def fake_run_git(cwd, args, *, timeout_seconds=60):
                calls.append((cwd, list(args), timeout_seconds))
                if args[:3] == ["checkout", "--force", "FETCH_HEAD"]:
                    return 0, "checked out"
                return 0, "ok"

            with (
                patch.dict(os.environ, {"MEP_REPO_AUDIT_GIT_TIMEOUT_SECONDS": "240"}),
                patch.object(wm, "_run_git", side_effect=fake_run_git),
            ):
                ok, path = wm.sync_repo_audit_workspace("github.com/WUAIBING/MEP", "main")

        self.assertTrue(ok)
        self.assertEqual(path, workspace_path)
        self.assertTrue(calls)
        self.assertTrue(all(timeout == 240 for _cwd, _args, timeout in calls))

    def test_render_structured_repo_audit_filters_findings_to_inventory(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed README and runtime entrypoints.",
                    "files_deep_read": ["README.md", "node/mep_runtime.py"],
                    "areas_not_deeply_reviewed": ["deployment scripts"],
                    "checks_performed": ["checked tracked file inventory", "read runtime entrypoint"],
                    "risk_areas_checked": ["runtime entrypoints", "repo contract drift"],
                    "findings": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path should fail closed on missing workspace context",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Repo audit must fail closed if workspace grounding is incomplete.",
                            "failure_mode": "A partial bootstrap can still reach result publication without an authoritative inventory.",
                            "proof_type": "code_path",
                            "fix_priority": "fix_now",
                            "developer_impact": "Otherwise the audit can publish an ungrounded result after a partial bootstrap failure.",
                            "evidence": "This file controls the audit workspace bootstrap path.",
                            "supporting_files": ["node/mep_runtime.py", "README.md"],
                            "same_file_check": "Checked the local publish guard in node/mep_runtime.py and did not find a same-file branch that permits publication after _repo_audit_contract_failure.",
                            "contradiction_check": "Checked the repo audit task contract description in README.md for a contradictory fail-open path and did not find one.",
                            "next_step": "Keep refusing repo_audit tasks whenever the workspace bootstrap or inventory load is incomplete.",
                        },
                        {
                            "file": "config.json",
                            "issue": "Hardcoded API keys",
                            "rationale": "This file does not exist and should be filtered out.",
                        },
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("node/mep_runtime.py", rendered)
        self.assertNotIn("config.json", rendered)
        self.assertIn("Files deep read: `README.md`, `node/mep_runtime.py`", rendered)
        self.assertIn("Areas not deeply reviewed: deployment scripts", rendered)
        self.assertIn("[high/high/fix_now] Runtime sync path should fail closed on missing workspace context", rendered)
        self.assertIn("Invariant: Repo audit must fail closed if workspace grounding is incomplete.", rendered)
        self.assertIn("Failure mode: A partial bootstrap can still reach result publication without an authoritative inventory.", rendered)
        self.assertIn("Proof: code_path", rendered)

    def test_render_structured_repo_audit_demotes_low_confidence_notes_to_observations(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed README and runtime entrypoints.",
                    "files_deep_read": ["node/mep_runtime.py"],
                    "findings": [
                        {
                            "file": "README.md",
                            "title": "README wording may drift from runtime behavior",
                            "category": "documentation",
                            "severity": "low",
                            "confidence": "low",
                            "developer_impact": "This is mostly a maintenance concern rather than a correctness blocker.",
                            "evidence": "The repository overview wording is broader than the runtime-specific safeguards reviewed here.",
                            "next_step": "Refresh the README only when the audited runtime contract changes again.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertIn("Observations: `README.md`: This is mostly a maintenance concern rather than a correctness blocker.", rendered)
        self.assertNotIn("[low/low]", rendered)

    def test_render_structured_repo_audit_rejects_non_invariant_finding_and_keeps_near_miss(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed README and runtime entrypoints.",
                    "files_deep_read": ["node/mep_runtime.py"],
                    "findings": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path may be risky",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "developer_impact": "This sounds important but does not explain the invariant.",
                            "evidence": "The file controls workspace bootstrap.",
                            "next_step": "Review it.",
                        }
                    ],
                    "near_misses": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path may be risky",
                            "reason_not_published": "The candidate did not explain a concrete invariant or failure mode from the supplied code path.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("Runtime sync path may be risky**", rendered)
        self.assertIn("Near misses: `node/mep_runtime.py` - Runtime sync path may be risky: The candidate did not explain a concrete invariant or failure mode from the supplied code path.", rendered)

    def test_render_structured_repo_audit_requires_findings_to_name_deep_read_files(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed README only.",
                    "files_deep_read": ["README.md"],
                    "findings": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path should fail closed on missing workspace context",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Repo audit must fail closed if workspace grounding is incomplete.",
                            "failure_mode": "A partial bootstrap can still reach result publication without an authoritative inventory.",
                            "proof_type": "code_path",
                            "fix_priority": "fix_now",
                            "developer_impact": "Otherwise the audit can publish an ungrounded result after a partial bootstrap failure.",
                            "evidence": "sync_repo_audit_workspace and _repo_audit_contract_failure gate the publish path.",
                            "next_step": "Keep refusing repo_audit tasks whenever the workspace bootstrap or inventory load is incomplete.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("[high/high/fix_now] Runtime sync path should fail closed on missing workspace context", rendered)
        self.assertIn(
            "Near misses: `node/mep_runtime.py` - Runtime sync path should fail closed on missing workspace context: The claim was withheld because the file was not listed in files_deep_read.",
            rendered,
        )

    def test_render_structured_repo_audit_high_severity_findings_require_cross_file_support(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "hub/main.py", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed runtime bootstrap and request wiring.",
                    "files_deep_read": ["hub/main.py", "node/mep_runtime.py"],
                    "findings": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path should fail closed on missing workspace context",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Repo audit must fail closed if workspace grounding is incomplete.",
                            "failure_mode": "A partial bootstrap can still reach result publication without an authoritative inventory.",
                            "proof_type": "cross_file_interaction",
                            "fix_priority": "fix_now",
                            "developer_impact": "Otherwise the audit can publish an ungrounded result after a partial bootstrap failure.",
                            "evidence": "sync_repo_audit_workspace prepares the workspace before the runtime publishes a result.",
                            "supporting_files": ["node/mep_runtime.py"],
                            "contradiction_check": "Checked the publish path for an enforcing caller but only verified the runtime file itself.",
                            "next_step": "Keep refusing repo_audit tasks whenever the workspace bootstrap or inventory load is incomplete.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("[high/high/fix_now] Runtime sync path should fail closed on missing workspace context", rendered)
        self.assertIn(
            "Near misses: `node/mep_runtime.py` - Runtime sync path should fail closed on missing workspace context: The claim was withheld because high-severity findings must cite at least one additional deep-read supporting file.",
            rendered,
        )

    def test_render_structured_repo_audit_high_severity_findings_require_same_file_check(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "hub/main.py", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed runtime bootstrap and publish gating.",
                    "files_deep_read": ["README.md", "node/mep_runtime.py", "hub/main.py"],
                    "findings": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path should fail closed on missing workspace context",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Repo audit must fail closed if workspace grounding is incomplete.",
                            "failure_mode": "A partial bootstrap can still reach result publication without an authoritative inventory.",
                            "proof_type": "cross_file_interaction",
                            "fix_priority": "fix_now",
                            "developer_impact": "Otherwise the audit can publish an ungrounded result after a partial bootstrap failure.",
                            "evidence": "sync_repo_audit_workspace prepares the workspace before the runtime publishes a result.",
                            "supporting_files": ["node/mep_runtime.py", "hub/main.py"],
                            "contradiction_check": "Checked the caller-side publish path in hub/main.py for an enforcing guard and did not find one.",
                            "next_step": "Keep refusing repo_audit tasks whenever the workspace bootstrap or inventory load is incomplete.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("[high/high/fix_now] Runtime sync path should fail closed on missing workspace context", rendered)
        self.assertIn(
            "Near misses: `node/mep_runtime.py` - Runtime sync path should fail closed on missing workspace context: The claim was withheld because high-severity findings must describe the same-file contradiction check that ruled out a nearby guard or branch.",
            rendered,
        )

    def test_render_structured_repo_audit_withholds_same_file_contradicted_high_severity_finding(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["bridge/github_to_mep.py", "docs/external-bridge/README.md", "README.md"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited bridge trigger enforcement and configuration docs.",
                    "repo_overview": "Reviewed bridge trigger gating and its documented operator controls.",
                    "files_deep_read": ["bridge/github_to_mep.py", "docs/external-bridge/README.md"],
                    "findings": [
                        {
                            "file": "bridge/github_to_mep.py",
                            "title": "MEP_BRIDGE_MAINTAINER_ONLY is not enforced",
                            "category": "automation/writeback safety",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Maintainer-only bridge mode must reject trigger authors whose GitHub association is not allowed.",
                            "failure_mode": "A non-maintainer can mention the bot and still cause automated bridge task submission.",
                            "proof_type": "cross_file_interaction",
                            "fix_priority": "fix_now",
                            "developer_impact": "That would let untrusted GitHub users trigger autonomous review and approval actions.",
                            "evidence": "The maintainer-only config is documented for the external bridge runtime.",
                            "supporting_files": ["bridge/github_to_mep.py", "docs/external-bridge/README.md"],
                            "same_file_check": "Checked the trigger extraction path in bridge/github_to_mep.py for a local author-association guard before trigger parsing.",
                            "same_file_contradicted_by": ["bridge/github_to_mep.py rejects disallowed author_association before extracting triggers"],
                            "contradiction_check": "Checked the docs in docs/external-bridge/README.md for the operator-facing maintainer-only requirement.",
                            "next_step": "Keep the author-association gate fail-closed before bot trigger extraction.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("[high/high/fix_now] MEP_BRIDGE_MAINTAINER_ONLY is not enforced", rendered)
        self.assertIn(
            "Near misses: `bridge/github_to_mep.py` - MEP_BRIDGE_MAINTAINER_ONLY is not enforced: The claim was withheld because same-file contradictory evidence remained (bridge/github_to_mep.py rejects disallowed author_association before extracting triggers).",
            rendered,
        )

    def test_render_structured_repo_audit_withholds_contradicted_high_severity_finding(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["hub/auth.py", "hub/main.py", "README.md"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited hub auth and request verification wiring.",
                    "repo_overview": "Reviewed auth helpers and request verification callers.",
                    "files_deep_read": ["hub/auth.py", "hub/main.py"],
                    "findings": [
                        {
                            "file": "hub/auth.py",
                            "title": "Signature verification accepts unregistered keys",
                            "category": "auth",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Every signed request must resolve the registered public key for the claimed node before verification.",
                            "failure_mode": "A caller can supply an arbitrary key and still pass signature verification for another node.",
                            "proof_type": "cross_file_interaction",
                            "fix_priority": "fix_now",
                            "developer_impact": "That would break node identity trust across authenticated hub requests.",
                            "evidence": "verify_signature only verifies the supplied public key bytes.",
                            "supporting_files": ["hub/auth.py", "hub/main.py"],
                            "same_file_check": "Checked hub/auth.py for a same-file registry lookup or fallback branch before verify_signature returns.",
                            "contradiction_check": "Checked verify_request in hub/main.py for a registry lookup before the auth helper is called.",
                            "contradicted_by": ["hub/main.py verify_request loads pub_pem via db.get_pub_pem(x_mep_nodeid) before calling verify_signature"],
                            "next_step": "Keep the registry lookup anchored in the request verification path.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("[high/high/fix_now] Signature verification accepts unregistered keys", rendered)
        self.assertIn(
            "Near misses: `hub/auth.py` - Signature verification accepts unregistered keys: The claim was withheld because contradictory workspace evidence remained (hub/main.py verify_request loads pub_pem via db.get_pub_pem(x_mep_nodeid) before calling verify_signature).",
            rendered,
        )

    def test_render_structured_repo_audit_withholds_findings_when_files_deep_read_is_missing(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed runtime bootstrap and publish gating.",
                    "findings": [
                        {
                            "file": "node/mep_runtime.py",
                            "title": "Runtime sync path should fail closed on missing workspace context",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "invariant": "Repo audit must fail closed if workspace grounding is incomplete.",
                            "failure_mode": "A partial bootstrap can still reach result publication without an authoritative inventory.",
                            "proof_type": "code_path",
                            "fix_priority": "fix_now",
                            "developer_impact": "Otherwise the audit can publish an ungrounded result after a partial bootstrap failure.",
                            "evidence": "sync_repo_audit_workspace and _repo_audit_contract_failure gate the publish path.",
                            "next_step": "Keep refusing repo_audit tasks whenever the workspace bootstrap or inventory load is incomplete.",
                        }
                    ],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("## Repo Audit Summary", rendered)
        self.assertNotIn("[high/high/fix_now] Runtime sync path should fail closed on missing workspace context", rendered)
        self.assertIn(
            "Near misses: `node/mep_runtime.py` - Runtime sync path should fail closed on missing workspace context: The claim was withheld because the file was not listed in files_deep_read.",
            rendered,
        )

    def test_render_structured_repo_audit_adds_default_coverage_summary_when_missing(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["README.md", "node/mep_runtime.py"],
                    }
                }
            },
        }
        rendered = mep_runtime._render_structured_repo_audit_with_task_data(  # noqa: SLF001
            json.dumps(
                {
                    "summary": "Audited the workspace-backed repo context.",
                    "repo_overview": "Reviewed README and runtime entrypoints.",
                    "findings": [],
                    "why_no_finding": "No grounded high-signal issue survived the evidence bar.",
                    "files_deep_read": ["node/mep_runtime.py"],
                    "areas_not_deeply_reviewed": ["deployment scripts"],
                }
            ),
            max_chars=4000,
            task_data=task_data,
        )

        self.assertIn("Coverage summary:", rendered)
        self.assertIn("Deep read: `node/mep_runtime.py`", rendered)
        self.assertIn("Not deeply reviewed: deployment scripts", rendered)

    def test_default_repo_audit_renderer_includes_repo_specific_risk_areas(self):
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "inventory_paths": ["hub/db.py", "node/mep_runtime.py", "bridge/github_to_mep.py"],
                    }
                }
            },
        }

        rendered = mep_runtime._render_default_repo_audit(  # noqa: SLF001
            task_data=task_data,
            max_chars=4000,
        )

        self.assertIn("Risk areas checked:", rendered)
        self.assertIn("fail-closed correctness around audit contracts", rendered)
        self.assertIn("automation/writeback safety, approval gating", rendered)

    def test_extract_repo_audit_candidates_filters_to_inventory_and_priority(self):
        reply = json.dumps(
            {
                "risk_candidates": [
                    {
                        "file": "config.json",
                        "category": "security",
                        "priority": "critical",
                        "claim": "Fake config issue",
                        "reason": "Should be dropped because the file is not in inventory.",
                        "evidence": ["config.json"],
                    },
                    {
                        "file": "node/mep_runtime.py",
                        "category": "correctness",
                        "priority": "high",
                        "claim": "Fail-open path may publish without grounding",
                        "reason": "The runtime owns repo-audit workspace bootstrap and publish gating.",
                        "evidence": ["sync_repo_audit_workspace", "_repo_audit_contract_failure"],
                    },
                    {
                        "file": "hub/db.py",
                        "category": "ledger",
                        "priority": "medium",
                        "claim": "Balance mutation may drift across storage paths",
                        "reason": "The storage code touches ledger updates in multiple code paths.",
                        "evidence": ["hub/db.py"],
                    },
                ]
            }
        )

        candidates = mep_runtime._extract_repo_audit_candidates(  # noqa: SLF001
            reply,
            allowed_paths=["hub/db.py", "node/mep_runtime.py"],
        )

        self.assertEqual([item["file"] for item in candidates], ["node/mep_runtime.py", "hub/db.py"])
        self.assertEqual(candidates[0]["priority"], "high")
        self.assertEqual(candidates[0]["claim"], "Fail-open path may publish without grounding.")

    def test_extract_repo_audit_candidate_packet_preserves_coverage(self):
        reply = json.dumps(
            {
                "risk_candidates": [
                    {
                        "file": "node/mep_runtime.py",
                        "category": "correctness",
                        "priority": "high",
                        "claim": "Fail-open audit path may publish without grounding",
                        "reason": "The runtime owns repo-audit workspace bootstrap and publish gating.",
                        "evidence": ["sync_repo_audit_workspace", "_repo_audit_contract_failure"],
                    }
                ],
                "coverage": ["read node/mep_runtime.py bootstrap path", "checked repo_audit publish gating"],
            }
        )

        packet = mep_runtime._extract_repo_audit_candidate_packet(  # noqa: SLF001
            reply,
            allowed_paths=["node/mep_runtime.py"],
        )

        self.assertEqual(len(packet["risk_candidates"]), 1)
        self.assertEqual(packet["coverage"], ["read node/mep_runtime.py bootstrap path", "checked repo_audit publish gating"])

    def test_deepseek_adapter_uses_two_pass_repo_audit_flow(self):
        adapter = mep_runtime.DeepSeekAdapter(api_key="secret-key", model="deepseek-chat")
        task_data = {
            "intent": {"type": "repo_audit.request"},
            "task": {
                "inputs": {
                    "repo_audit": {
                        "repo_url": "github.com/WUAIBING/MEP",
                        "ref": "main",
                        "local_workspace_path": "/tmp/mep-audit",
                        "inventory_paths": ["hub/db.py", "node/mep_runtime.py"],
                    }
                }
            },
        }
        fake_responses = [
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"risk_candidates":['
                                    '{"file":"node/mep_runtime.py","category":"correctness","priority":"high",'
                                    '"claim":"Fail-open audit path may publish without grounding",'
                                    '"reason":"Workspace bootstrap and publish gating live in the same runtime file.",'
                                    '"evidence":["sync_repo_audit_workspace","_repo_audit_contract_failure"]}'
                                    '],"coverage":["repo audit runtime and contract path"]}'
                                )
                            }
                        }
                    ]
                },
            ),
            _FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"summary":"Audited the repo-audit runtime path.","repo_overview":"Reviewed the runtime bootstrap path.",'
                                    '"files_deep_read":["node/mep_runtime.py"],'
                                    '"risk_areas_checked":["workspace grounding"],'
                                    '"checks_performed":["verified the runtime only publishes from tracked inventory"],'
                                    '"why_no_finding":"The grounded workspace evidence did not prove a concrete fail-open publish path.",'
                                    '"findings":[],"near_misses":[{"file":"node/mep_runtime.py","title":"Fail-open audit path may publish without grounding",'
                                    '"reason_not_published":"The candidate remained plausible but did not clear the published-evidence bar from the supplied workspace excerpts."}],'
                                    '"observations":[],"artifact_recommended":false}'
                                )
                            }
                        }
                    ]
                },
            ),
        ]

        with patch("node.mep_runtime.requests.post", side_effect=fake_responses) as post_mock:
            reply = adapter.generate_reply("Audit the repository", task_data)

        self.assertIn("## Repo Audit Summary", reply)
        self.assertIn("Risk areas checked: workspace grounding", reply)
        self.assertIn("Near misses: `node/mep_runtime.py` - Fail-open audit path may publish without grounding", reply)
        self.assertEqual(post_mock.call_count, 2)
        first_system_prompt = post_mock.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        second_system_prompt = post_mock.call_args_list[1].kwargs["json"]["messages"][0]["content"]
        second_user_payload = post_mock.call_args_list[1].kwargs["json"]["messages"][1]["content"]
        self.assertIn("candidate-generation pass for a MEP repository audit", first_system_prompt)
        self.assertIn("This is the verification pass.", second_system_prompt)
        self.assertIn("Candidate repo-audit material to verify before publishing any finding", second_user_payload)
        self.assertIn('"candidate_coverage"', second_user_payload)

    def test_clean_check_env_uses_allowlist_and_throwaway_home(self):
        with patch.dict(
            os.environ,
            {
                "MEP_REVIEW_RUN_CHECKS": "true",
                "MEP_AI_MAX_TOKENS": "4000",
                "DEEPSEEK_API_KEY": "super-secret",
                "R2_SECRET_ACCESS_KEY": "hidden",
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": "repo",
            },
        ):
            env = mep_runtime.WorkspaceManager._clean_check_env("C:/tmp/mep-review-home")  # noqa: SLF001
        self.assertNotIn("MEP_REVIEW_RUN_CHECKS", env)
        self.assertNotIn("MEP_AI_MAX_TOKENS", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("R2_SECRET_ACCESS_KEY", env)
        self.assertIn("PATH", env)
        self.assertEqual(env["HOME"], "C:/tmp/mep-review-home")
        self.assertEqual(env["USERPROFILE"], "C:/tmp/mep-review-home")
        self.assertEqual(env["PYTHONPATH"], "repo")

    def test_build_verification_report_runs_pytest_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
            with open(os.path.join(tmp, "tests", "test_x.py"), "w", encoding="utf-8") as handle:
                handle.write("def test_x():\n    assert True\n")
            wm = mep_runtime.WorkspaceManager(tmp)
            with patch.object(wm, "_run_check", return_value=(0, "1 passed in 0.01s")) as run_mock:
                report = wm.build_verification_report(
                    tmp,
                    ["node/x.py"],
                    ["tests/test_x.py"],
                    enabled=True,
                )

        self.assertIn("Automated verification run on the checked-out PR head", report)
        self.assertIn("pytest (changed tests): passed", report)
        self.assertIn("1 passed", report)
        invoked = [call.args[1] for call in run_mock.call_args_list]
        self.assertTrue(any("tests/test_x.py" in args for args in invoked))

    def test_build_verification_report_skips_missing_or_deleted_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
            with open(os.path.join(tmp, "tests", "test_present.py"), "w", encoding="utf-8") as handle:
                handle.write("def test_present():\n    assert True\n")
            wm = mep_runtime.WorkspaceManager(tmp)
            with patch.object(wm, "_run_check", return_value=(0, "1 passed in 0.01s")) as run_mock:
                report = wm.build_verification_report(
                    tmp,
                    ["deleted.py", "tests/test_present.py"],
                    ["tests/test_present.py", "tests/test_removed.py"],
                    enabled=True,
                )

        self.assertIn("pytest (changed tests): passed", report)
        invoked = [call.args[1] for call in run_mock.call_args_list]
        flattened = " ".join(" ".join(args) for args in invoked)
        self.assertIn("tests/test_present.py", flattened)
        self.assertNotIn("deleted.py", flattened)
        self.assertNotIn("tests/test_removed.py", flattened)


class TestAgenticReviewLoopLeakGuard(unittest.TestCase):
    """The agentic review loop must never publish raw model reasoning."""

    def _run(self, responses, *, review_max_chars=4000):
        calls = {"i": 0}
        sent_messages = {"last": None}

        def _invoke(messages, *, tools):
            sent_messages["last"] = list(messages)
            idx = calls["i"]
            calls["i"] += 1
            return responses[min(idx, len(responses) - 1)]

        result = mep_runtime._run_agentic_tool_loop(  # noqa: SLF001
            messages=[{"role": "user", "content": "review this"}],
            tools=mep_runtime._agentic_review_tools(),  # noqa: SLF001
            tools_aware_invoke=_invoke,
            workspace=None,
            workspace_path="",
            runtime_tool_runs=[],
            max_tool_calls=6,
            review_max_chars=review_max_chars,
        )
        return result, calls["i"], sent_messages["last"]

    def test_free_text_reasoning_is_not_published(self):
        scratchpad = "Let me analyze this PR carefully. I need to check the caller."
        # Every turn returns free-text reasoning and never calls submit_review.
        result, invocations, _ = self._run([{"content": scratchpad, "tool_calls": []}])
        self.assertEqual(result, "")
        # First free-text turn triggers a nudge, second gives up: two invocations.
        self.assertEqual(invocations, 2)

    def test_free_text_turn_nudges_toward_submit_review(self):
        result, _invocations, last_messages = self._run(
            [{"content": "Let me look at the diff first.", "tool_calls": []}]
        )
        self.assertEqual(result, "")
        nudge = last_messages[-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("submit_review", nudge["content"])

    def test_clean_free_text_review_is_published_after_nudge(self):
        # deepseek and similar models often return a finished, structured review
        # as free text instead of calling submit_review. That review must still
        # be published (regression guard: the leak fix must not silence it).
        clean = (
            "## Review Summary\n\nReviewed `hub/db.py` get_pub_pem() and its call "
            "sites. The pending_registrations guard is correct and connections are "
            "released on the early-return path. No blocking issues. Approve."
        )
        result, invocations, _ = self._run([{"content": clean, "tool_calls": []}])
        self.assertEqual(result, clean)
        # First free-text turn nudges, second publishes the clean review.
        self.assertEqual(invocations, 2)

    def test_submit_review_summary_is_published(self):
        summary = "## Review Summary\n\nChecked the changed handler and its call sites."
        response = {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps({"summary": summary, "approval": True}),
                    },
                }
            ],
        }
        result, _invocations, _ = self._run([response])
        self.assertEqual(result, summary)

    def test_submit_review_without_summary_does_not_leak_content(self):
        response = {
            "content": "Let me think about whether to approve. I need to verify the guard.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps({"approval": True}),
                    },
                }
            ],
        }
        result, _invocations, _ = self._run([response])
        self.assertEqual(result, "")

    def test_scratchpad_detector_flags_reasoning(self):
        self.assertTrue(
            mep_runtime._looks_like_agent_scratchpad(  # noqa: SLF001
                "Let me analyze this PR carefully. I need to check the caller."
            )
        )
        self.assertTrue(
            mep_runtime._looks_like_agent_scratchpad(  # noqa: SLF001
                "From workspace_search I can see the call site at line 3533."
            )
        )
        # Adverb between the planning stem and the verb must still be caught
        # ("let me CAREFULLY analyze", "first, let me re-read", "I'm going to verify").
        for leak in (
            "Let me carefully analyze the PR diff and workspace context.",
            "First, let me re-read the diff before deciding.",
            "I'll now examine the changed lines.",
            "I'm going to verify the behavior of the guard.",
            "Now let me look at the callers.",
        ):
            self.assertTrue(
                mep_runtime._looks_like_agent_scratchpad(leak),  # noqa: SLF001
                msg=f"expected scratchpad: {leak!r}",
            )

    def test_scratchpad_detector_allows_grounded_review(self):
        grounded = (
            "## Review Summary\n\nReviewed the changed behavior around "
            "`_render_review_telemetry_footer` and did not find a concrete issue "
            "supported by the diff. Touched paths reviewed: `bridge/github_to_mep.py`."
        )
        self.assertFalse(mep_runtime._looks_like_agent_scratchpad(grounded))  # noqa: SLF001
        # A closing sign-off that merely contains "let me know" is not scratchpad.
        self.assertFalse(
            mep_runtime._looks_like_agent_scratchpad(  # noqa: SLF001
                "Let me know if you have any questions about this review."
            )
        )


if __name__ == "__main__":
    unittest.main()
