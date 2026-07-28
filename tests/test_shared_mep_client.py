import asyncio
import json
import os
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch

from clients.shared.mep_client import MEPClient
from clients.shared.purchase_policy import OwnerPurchasePolicy


class _FakeIdentity:
    node_id = "node_consumer"
    pub_pem = "pub"

    def get_auth_headers(self, payload: str) -> dict:
        return {"X-MEP-NodeID": self.node_id, "X-MEP-Signature": "sig"}

    def sign(self, node_id: str, timestamp: str) -> str:
        return f"sig:{node_id}:{timestamp}"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {"status": "success", "task_id": "task_123456"}

    def json(self) -> dict:
        return self._json_data


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class _ScriptedWebSocket(_FakeWebSocket):
    def __init__(self, messages):
        super().__init__()
        self.messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def recv(self):
        return json.dumps(self.messages.pop(0))


class _SelectivelyFailingWebSocket(_FakeWebSocket):
    def __init__(self, failing_contexts):
        super().__init__()
        self.failing_contexts = set(failing_contexts)
        self.attempted = []

    async def send(self, payload: str) -> None:
        context_id = json.loads(payload)["context_id"]
        self.attempted.append(context_id)
        if context_id in self.failing_contexts:
            raise ConnectionError(f"send failed for {context_id}")
        await super().send(payload)


class TestSharedMEPClient(unittest.TestCase):
    def test_websocket_uri_binds_takeover_to_v1_signature(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch.dict(os.environ, {"MEP_WS_TAKEOVER": "1"}, clear=False),
        ):
            client = MEPClient("unused.pem")
            uri = urllib.parse.urlparse(client.websocket_uri())

        query = urllib.parse.parse_qs(uri.query)
        self.assertEqual(query["lease_protocol"], ["v1"])
        self.assertEqual(query["takeover"], ["1"])
        self.assertEqual(
            query["signature"],
            [f"sig:node_consumer|lease=v1|takeover=1:{query['timestamp'][0]}"],
        )

    def test_legacy_websocket_uri_supports_rolling_hub_upgrade(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch.dict(os.environ, {"MEP_WS_LEASE_PROTOCOL": "legacy"}, clear=False),
        ):
            client = MEPClient("unused.pem")
            uri = urllib.parse.urlparse(client.websocket_uri())

        query = urllib.parse.parse_qs(uri.query)
        self.assertNotIn("lease_protocol", query)
        self.assertNotIn("takeover", query)
        self.assertEqual(
            query["signature"],
            [f"sig:node_consumer:{query['timestamp'][0]}"],
        )

    def test_connection_ready_binds_task_results_to_current_lease(self):
        with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
            client = MEPClient("unused.pem")

        consumed = client.observe_connection_event(
            {
                "event": "connection.ready",
                "connection_id": "connection-123",
                "epoch": 7,
            }
        )

        self.assertTrue(consumed)
        self.assertEqual(client.connection_id, "connection-123")
        self.assertEqual(client.connection_epoch, 7)
        self.assertEqual(
            client.bind_connection_lease({"task_id": "task-1"}),
            {"task_id": "task-1", "connection_id": "connection-123"},
        )

    def test_listener_consumes_lease_handshake_before_application_events(self):
        ws = _ScriptedWebSocket(
            [
                {
                    "event": "connection.ready",
                    "connection_id": "connection-123",
                    "epoch": 3,
                },
                {"event": "call.ping", "context_id": "ctx-ping"},
            ]
        )
        observed = []

        async def _run():
            with (
                patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
                patch("clients.shared.mep_client.ws_connect", return_value=ws),
            ):
                client = MEPClient("unused.pem")
                client.heartbeat_seconds = 0

                async def on_result(_data):
                    return None

                async def on_event(data):
                    observed.append(data)
                    client.stop()

                await client.listen_results(on_result, on_event)
                return client

        client = asyncio.run(_run())
        self.assertEqual(client.connection_id, "connection-123")
        self.assertEqual(client.connection_epoch, 3)
        self.assertEqual([event["event"] for event in observed], ["call.ping"])

    def test_submit_task_uses_spec_shaped_envelope(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            response = asyncio.run(
                client.submit_task(
                    "summarize this",
                    1.5,
                    model_requirement="text",
                    target_node="node_provider",
                )
            )

        self.assertEqual(response["json"]["task_id"], "task_123456")
        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(body["source"], {"node_id": "node_consumer"})
        self.assertEqual(body["task"]["instructions"], "summarize this")
        self.assertEqual(body["task"]["expected_output"], {"result_type": "text"})
        self.assertEqual(
            body["economics"],
            {
                "bounty_ns": 1_500_000_000,
                "currency": "MEP_NS",
                "market": "compute",
                "payment_direction": "sender_to_receiver",
            },
        )
        self.assertEqual(body["routing"], {"target_node_id": "node_provider", "target_capability": "text"})

    def test_get_balance_ns_uses_canonical_v2_endpoint(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.get.return_value = _FakeResponse(
                json_data={
                    "node_id": "node_consumer",
                    "balance_ns": "10000000000",
                    "currency": "MEP_NS",
                }
            )
            client = MEPClient("unused.pem")

            result = asyncio.run(client.get_balance_ns())

        self.assertEqual(result["json"]["balance_ns"], "10000000000")
        self.assertTrue(session.get.call_args.args[0].endswith("/v2/balance/node_consumer"))

    def test_submit_compute_task_ns_enforces_policy_and_uses_v2_wire_amount(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.get.return_value = _FakeResponse(
                json_data={
                    "node_id": "node_consumer",
                    "balance_ns": "10000000000",
                    "currency": "MEP_NS",
                }
            )
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")
            policy = OwnerPurchasePolicy.from_mapping(
                {
                    "max_total_price_ns": "3000000000",
                    "max_price_per_provider_ns": "3000000000",
                    "minimum_reserve_ns": "2000000000",
                    "currency": "MEP_NS",
                }
            )

            result = asyncio.run(
                client.submit_compute_task_ns(
                    "Review exact PR head",
                    "3000000000",
                    policy=policy,
                    model_requirement="code_review",
                    target_node="node_provider",
                    idempotency_key="purchase-001",
                )
            )

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["json"]["purchase_authorization"]["status"], "approved")
        self.assertTrue(session.post.call_args.args[0].endswith("/v2/tasks/submit"))
        self.assertEqual(
            session.post.call_args.kwargs["headers"]["X-MEP-Idempotency-Key"],
            "purchase-001",
        )
        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(
            body["economics"],
            {
                "bounty_ns": "3000000000",
                "currency": "MEP_NS",
                "market": "compute",
                "payment_direction": "sender_to_receiver",
            },
        )
        self.assertEqual(
            body["routing"],
            {
                "target_node_id": "node_provider",
                "target_capability": "code_review",
            },
        )

    def test_submit_compute_task_ns_fails_closed_before_posting(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.get.return_value = _FakeResponse(
                json_data={
                    "node_id": "node_consumer",
                    "balance_ns": "10000000000",
                    "currency": "MEP_NS",
                }
            )
            client = MEPClient("unused.pem")
            policy = OwnerPurchasePolicy.from_mapping(
                {
                    "max_total_price_ns": "2000000000",
                    "max_price_per_provider_ns": "2000000000",
                    "currency": "MEP_NS",
                }
            )

            result = asyncio.run(
                client.submit_compute_task_ns(
                    "Too expensive",
                    "3000000000",
                    policy=policy,
                )
            )

        self.assertEqual(result["status_code"], 403)
        self.assertEqual(result["json"]["reason"], "per_provider_price_limit_exceeded")
        session.post.assert_not_called()

    def test_concurrent_compute_submissions_serialize_reserve_preflight(self):
        class _BalanceAwareSession:
            def __init__(self):
                self.trust_env = False
                self.post_count = 0
                self.lock = threading.Lock()

            def get(self, *_args, **_kwargs):
                with self.lock:
                    balance_ns = 10_000_000_000 - self.post_count * 3_000_000_000
                return _FakeResponse(
                    json_data={
                        "node_id": "node_consumer",
                        "balance_ns": str(balance_ns),
                        "currency": "MEP_NS",
                    }
                )

            def post(self, *_args, **_kwargs):
                time.sleep(0.05)
                with self.lock:
                    self.post_count += 1
                return _FakeResponse()

        session = _BalanceAwareSession()
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session", return_value=session),
        ):
            client = MEPClient("unused.pem")
            policy = OwnerPurchasePolicy.from_mapping(
                {
                    "max_total_price_ns": "3000000000",
                    "max_price_per_provider_ns": "3000000000",
                    "minimum_reserve_ns": "5000000000",
                    "currency": "MEP_NS",
                }
            )

            async def run_both():
                return await asyncio.gather(
                    client.submit_compute_task_ns(
                        "first",
                        "3000000000",
                        policy=policy,
                    ),
                    client.submit_compute_task_ns(
                        "second",
                        "3000000000",
                        policy=policy,
                    ),
                )

            first, second = asyncio.run(run_both())

        self.assertEqual(first["status_code"], 200)
        self.assertEqual(second["status_code"], 403)
        self.assertEqual(second["json"]["reason"], "insufficient_spendable_balance")
        self.assertEqual(session.post_count, 1)

    def test_action_context_helpers_use_signed_persistent_endpoints(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse(json_data={"status": "created"})
            session.get.return_value = _FakeResponse(json_data={"events": []})
            client = MEPClient("unused.pem")

            created = asyncio.run(
                client.create_action_context(
                    ["node_hub", "node_elsaws"],
                    topic="Parallel review",
                    context_id="action-context-123",
                    max_events=250,
                )
            )
            posted = asyncio.run(
                client.post_action_event(
                    "action-context-123",
                    "review-client",
                    "action.progress",
                    event_id="evt-progress-123",
                    phase="workspace_read",
                    message="Read the changed files.",
                    progress=40,
                )
            )
            replay = asyncio.run(
                client.get_action_context(
                    "action-context-123",
                    after_seq=7,
                    limit=25,
                )
            )

        self.assertEqual(created["status_code"], 200)
        self.assertEqual(posted["status_code"], 200)
        self.assertEqual(replay["status_code"], 200)
        create_body = json.loads(session.post.call_args_list[0].kwargs["data"])
        self.assertEqual(create_body["owner_id"], "node_consumer")
        self.assertEqual(create_body["participants"], ["node_hub", "node_elsaws"])
        self.assertEqual(create_body["context_id"], "action-context-123")
        event_body = json.loads(session.post.call_args_list[1].kwargs["data"])
        self.assertEqual(event_body["event_type"], "action.progress")
        self.assertEqual(event_body["progress"], 40)
        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {"after_seq": 7, "limit": 25},
        )

        metadata = MEPClient.build_action_context_metadata(
            "action-context-123",
            "review-client",
            parent_action_id="review-parent",
        )
        self.assertEqual(
            metadata,
            {
                "spec_version": "mep.action.v1",
                "context_id": "action-context-123",
                "action_id": "review-client",
                "parent_action_id": "review-parent",
            },
        )

    def test_send_ws_event_uses_active_socket_when_present(self):
        with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
            client = MEPClient("unused.pem")
            ws = _FakeWebSocket()
            client._active_ws = ws

            sent = asyncio.run(client.send_ws_event({"event": "call.accept", "context_id": "ctx-1"}))

        self.assertTrue(sent)
        self.assertEqual(json.loads(ws.sent[0]), {"event": "call.accept", "context_id": "ctx-1"})
        self.assertEqual(client._live_call_contexts, {"ctx-1"})

    def test_send_ws_event_returns_false_without_active_socket(self):
        with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
            client = MEPClient("unused.pem")

            sent = asyncio.run(client.send_ws_event({"event": "call.accept", "context_id": "ctx-1"}))

        self.assertFalse(sent)

    def test_listener_automatically_pongs_live_call_health_checks(self):
        ws = _ScriptedWebSocket(
            [{"event": "call.ping", "context_id": "ctx-ping"}]
        )
        observed = []

        async def _run():
            with (
                patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
                patch("clients.shared.mep_client.ws_connect", return_value=ws),
            ):
                client = MEPClient("unused.pem")
                client.heartbeat_seconds = 0

                async def on_result(_data):
                    return None

                async def on_event(data):
                    observed.append(data)
                    client.stop()

                await client.listen_results(on_result, on_event)

        asyncio.run(_run())
        self.assertEqual(
            json.loads(ws.sent[0]),
            {"event": "call.pong", "context_id": "ctx-ping"},
        )
        self.assertEqual(observed[0]["event"], "call.ping")

    def test_listener_resumes_active_calls_after_websocket_reconnect(self):
        ws = _ScriptedWebSocket(
            [{"event": "call.hangup", "context_id": "ctx-resume"}]
        )

        async def _run():
            with (
                patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
                patch("clients.shared.mep_client.ws_connect", return_value=ws),
            ):
                client = MEPClient("unused.pem")
                client.heartbeat_seconds = 0
                client._live_call_contexts.add("ctx-resume")

                async def on_result(_data):
                    return None

                async def on_event(_data):
                    client.stop()

                await client.listen_results(on_result, on_event)
                self.assertEqual(client._live_call_contexts, set())

        asyncio.run(_run())
        self.assertEqual(
            json.loads(ws.sent[0]),
            {"event": "call.resume", "context_id": "ctx-resume"},
        )

    def test_listener_reconnect_cycle_resumes_only_the_active_call(self):
        first_ws = _ScriptedWebSocket(
            [{"event": "call.accepted", "context_id": "ctx-cycle"}]
        )
        second_ws = _ScriptedWebSocket(
            [{"event": "call.resumed", "context_id": "ctx-cycle"}]
        )
        observed = []

        async def _run():
            with (
                patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
                patch(
                    "clients.shared.mep_client.ws_connect",
                    side_effect=[first_ws, second_ws],
                ),
            ):
                client = MEPClient("unused.pem")
                client.heartbeat_seconds = 0

                async def on_result(_data):
                    return None

                async def on_event(data):
                    observed.append(data["event"])
                    if data["event"] == "call.resumed":
                        client.stop()

                await client.listen_results(on_result, on_event)
                self.assertEqual(client._live_call_contexts, {"ctx-cycle"})

        asyncio.run(_run())
        self.assertEqual(observed, ["call.accepted", "call.resumed"])
        self.assertEqual(
            json.loads(second_ws.sent[0]),
            {"event": "call.resume", "context_id": "ctx-cycle"},
        )

    def test_unacknowledged_resume_is_evicted(self):
        async def _run():
            with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
                client = MEPClient("unused.pem")
                client.call_resume_ack_timeout_seconds = 0.01
                client._remember_live_call("ctx-stale")
                ws = _FakeWebSocket()

                await client._resume_live_calls(ws)
                await asyncio.sleep(0.03)

                self.assertEqual(client._live_call_contexts, set())
                self.assertEqual(client._call_resume_pending, {})

        asyncio.run(_run())

    def test_resume_send_failure_does_not_skip_remaining_calls(self):
        async def _run():
            with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
                client = MEPClient("unused.pem")
                client.call_resume_ack_timeout_seconds = 60
                for context_id in ("ctx-a", "ctx-b", "ctx-c"):
                    client._remember_live_call(context_id)
                ws = _SelectivelyFailingWebSocket({"ctx-b"})

                await client._resume_live_calls(ws)

                self.assertEqual(ws.attempted, ["ctx-a", "ctx-b", "ctx-c"])
                self.assertEqual(
                    [json.loads(payload)["context_id"] for payload in ws.sent],
                    ["ctx-a", "ctx-c"],
                )
                self.assertEqual(
                    set(client._call_resume_pending),
                    {"ctx-a", "ctx-c"},
                )
                client.stop()
                await asyncio.sleep(0)

        asyncio.run(_run())

    def test_failed_resume_send_survives_ack_timeout_for_next_connection(self):
        async def _run():
            with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
                client = MEPClient("unused.pem")
                client.call_resume_ack_timeout_seconds = 0.01
                client._remember_live_call("ctx-retry")

                await client._resume_live_calls(_SelectivelyFailingWebSocket({"ctx-retry"}))
                await asyncio.sleep(0.03)

                self.assertEqual(client._live_call_contexts, {"ctx-retry"})
                self.assertEqual(client._call_resume_pending, {})
                retry_ws = _FakeWebSocket()
                await client._resume_live_calls(retry_ws)
                self.assertEqual(
                    json.loads(retry_ws.sent[0]),
                    {"event": "call.resume", "context_id": "ctx-retry"},
                )
                client.stop()
                await asyncio.sleep(0)

        asyncio.run(_run())

    def test_resume_includes_call_added_while_sends_are_in_progress(self):
        async def _run():
            with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
                client = MEPClient("unused.pem")
                client.call_resume_ack_timeout_seconds = 60
                client._remember_live_call("ctx-a")

                class _AddingWebSocket(_FakeWebSocket):
                    async def send(self, payload: str) -> None:
                        await super().send(payload)
                        client._remember_live_call("ctx-b")

                ws = _AddingWebSocket()
                await client._resume_live_calls(ws)

                self.assertEqual(
                    [json.loads(payload)["context_id"] for payload in ws.sent],
                    ["ctx-a", "ctx-b"],
                )
                client.stop()
                await asyncio.sleep(0)

        asyncio.run(_run())

    def test_resume_drain_is_bounded_when_calls_keep_arriving(self):
        async def _run():
            with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
                client = MEPClient("unused.pem")
                client.call_context_max = 3
                client.call_resume_ack_timeout_seconds = 60
                client._remember_live_call("ctx-0")

                class _ContinuouslyAddingWebSocket(_FakeWebSocket):
                    async def send(self, payload: str) -> None:
                        await super().send(payload)
                        client._remember_live_call(f"ctx-{len(self.sent)}")

                ws = _ContinuouslyAddingWebSocket()
                await client._resume_live_calls(ws)

                self.assertEqual(len(ws.sent), client.call_context_max)
                client.stop()
                await asyncio.sleep(0)

        asyncio.run(_run())

    def test_live_call_tracking_is_ttl_and_size_bounded(self):
        with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
            client = MEPClient("unused.pem")
            client.call_context_max = 2
            client.call_context_ttl_seconds = 10
            client._live_call_contexts.update({"ctx-old", "ctx-mid", "ctx-new"})
            client._live_call_last_seen.update(
                {"ctx-old": 1.0, "ctx-mid": 5.0, "ctx-new": 9.0}
            )

            client._prune_live_calls(now=10.0)
            self.assertEqual(client._live_call_contexts, {"ctx-mid", "ctx-new"})

            client._prune_live_calls(now=20.0)
            self.assertEqual(client._live_call_contexts, set())

    def test_submit_task_can_send_secret_data_offer(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            asyncio.run(client.submit_task("Data offer available", -0.25, secret_data="encrypted-data"))

        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(
            body["economics"],
            {
                "bounty_ns": 250_000_000,
                "currency": "MEP_NS",
                "market": "data",
                "payment_direction": "receiver_to_sender",
            },
        )
        self.assertEqual(body["secret_data"], "encrypted-data")

    def test_submit_repo_audit_builds_structured_repo_audit_request(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            response = asyncio.run(
                client.submit_repo_audit(
                    "github.com/WUAIBING/MEP",
                    audit_type="architecture_audit",
                    ref="main",
                    max_findings=7,
                    inline_summary_max_chars=7000,
                    artifact_preference="artifact_preferred",
                    target_node="node_repo_bot",
                )
            )

        self.assertEqual(response["json"]["task_id"], "task_123456")
        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(body["intent"], {"type": "repo_audit.request", "priority": "high"})
        self.assertEqual(body["routing"], {"target_node_id": "node_repo_bot", "target_capability": "repo_audit"})
        self.assertEqual(body["task"]["title"], "Repo audit: github.com/WUAIBING/MEP")
        self.assertEqual(
            body["task"]["inputs"],
            {
                "repo_audit": {
                    "repo_url": "github.com/WUAIBING/MEP",
                    "audit_type": "architecture_audit",
                    "max_findings": 7,
                    "artifact_preference": "artifact_preferred",
                    "inline_summary_max_chars": 7000,
                    "ref": "main",
                }
            },
        )
        self.assertEqual(
            body["task"]["expected_output"],
            {
                "result_type": "repo_audit_result",
                "format": "json",
                "artifact_allowed": True,
                "inline_summary_max_chars": 7000,
            },
        )
        self.assertIn("architecture_audit repo audit", body["task"]["instructions"])
        self.assertIn("ref main", body["task"]["instructions"])
        self.assertIn("include its URI", body["task"]["instructions"])

    def test_submit_task_preserves_200_error_body(self):
        response_body = {"status": "error", "detail": "Target node not currently connected to Hub"}
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse(200, response_body)
            client = MEPClient("unused.pem")

            response = asyncio.run(client.submit_task("hello", 0.0, target_node="node_offline"))

        self.assertEqual(response["status_code"], 200)
        self.assertEqual(response["json"], response_body)

    def test_submit_dm_builds_threaded_interbot_envelope(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            response = asyncio.run(
                client.submit_dm(
                    "Please review PR 154",
                    "node_reviewer",
                    intent_type="review.request",
                    context_id="pr154-review",
                    reply_to_task_id="task_parent",
                    reply_to_message_id="message_parent",
                    turn_type="review_request",
                    turn_index=1,
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(submit_body["routing"], {"target_node_id": "node_reviewer"})
        self.assertEqual(submit_body["intent"], {"type": "review.request", "priority": "normal"})
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(body["spec_version"], "mep.interbot.v1")
        self.assertEqual(body["target"]["node_id"], "node_reviewer")
        self.assertEqual(body["conversation"]["context_id"], "pr154-review")
        self.assertEqual(body["conversation"]["reply_to_task_id"], "task_parent")
        self.assertEqual(body["conversation"]["reply_to_message_id"], "message_parent")
        self.assertEqual(body["conversation"]["turn_type"], "review_request")
        self.assertEqual(body["conversation"]["turn_index"], 1)
        self.assertEqual(body["intent"], {"type": "review.request", "priority": "normal"})
        self.assertEqual(body["task"]["instructions"], "Please review PR 154")
        self.assertEqual(body["delivery"], {"reply_mode": "new_dm", "settlement_mode": "task_result"})
        self.assertEqual(response["context_id"], "pr154-review")
        self.assertTrue(response["message_id"])
        self.assertTrue(response["trace_id"])

    def test_submit_dm_preserves_chat_intent_in_outer_task(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            asyncio.run(client.submit_dm("Hello", "node_peer", priority="low"))

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(submit_body["intent"], {"type": "chat.request", "priority": "low"})

    def test_submit_dm_can_attach_session_safety_metadata(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            asyncio.run(
                client.submit_dm(
                    "Stay inside the review lane.",
                    "node_reviewer",
                    context_id="pr154-review",
                    turn_type="review_request",
                    session_safety={"max_turns": 6, "max_duration_seconds": 900, "checkpoint_interval": 3},
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        session_safety = body["task"]["inputs"]["session_safety"]
        self.assertEqual(session_safety["max_turns"], 6)
        self.assertEqual(session_safety["max_duration_seconds"], 900)
        self.assertEqual(session_safety["checkpoint_interval"], 3)
        self.assertEqual(session_safety["started_at_ms"], body["timestamp_ms"])

    def test_submit_dm_can_attach_governance_metadata(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            asyncio.run(
                client.submit_dm(
                    "Need a redacted workspace summary.",
                    "node_reviewer",
                    context_id="gov-ctx",
                    governance=client.build_governance_metadata(
                        classification="approval_required",
                        reason="share redacted workspace facts only",
                        disclosure_scope=["workspace_summary"],
                        redaction_applied=True,
                        approval_status="approved",
                        approval_context_id="approval-ctx",
                        approved_by="node_human",
                    ),
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(
            body["task"]["inputs"]["governance"],
            {
                "classification": "approval_required",
                "reason": "share redacted workspace facts only",
                "disclosure_scope": ["workspace_summary"],
                "redaction_applied": True,
                "approval": {
                    "status": "approved",
                    "context_id": "approval-ctx",
                    "approved_by": "node_human",
                },
            },
        )

    def test_extract_interbot_instructions_prefers_structured_task_text(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "task": {
                    "instructions": "Structured instructions",
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        instructions, parsed = MEPClient.extract_interbot_instructions(payload)

        self.assertEqual(instructions, "Structured instructions")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["spec_version"], "mep.interbot.v1")

    def test_submit_review_verdict_dm_builds_structured_verdict_payload(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            response = asyncio.run(
                client.submit_review_verdict_dm(
                    "approve_with_conditions",
                    "Threading model is sound, but docs should mention ack expectations.",
                    "node_governor",
                    context_id="pr154-review",
                    reply_to_task_id="task_review_request",
                    reply_to_message_id="message_review_request",
                    conditions=["Document expected ack behavior", "Keep reply_mode=new_dm"],
                    human_recommendation="Merge after the follow-up docs note lands.",
                    turn_index=2,
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(body["intent"], {"type": "review.response", "priority": "normal"})
        self.assertEqual(body["conversation"]["turn_type"], "approval")
        self.assertEqual(body["conversation"]["turn_index"], 2)
        self.assertEqual(body["conversation"]["context_id"], "pr154-review")
        self.assertEqual(body["conversation"]["reply_to_task_id"], "task_review_request")
        self.assertEqual(body["conversation"]["reply_to_message_id"], "message_review_request")
        self.assertEqual(body["task"]["title"], "Review verdict")
        self.assertEqual(
            body["task"]["inputs"]["review_verdict"],
            {
                "decision": "approve_with_conditions",
                "rationale": "Threading model is sound, but docs should mention ack expectations.",
                "conditions": ["Document expected ack behavior", "Keep reply_mode=new_dm"],
                "human_recommendation": "Merge after the follow-up docs note lands.",
            },
        )
        self.assertEqual(response["context_id"], "pr154-review")

    def test_extract_review_verdict_reads_structured_verdict_payload(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "task": {
                    "instructions": "Review verdict: approve",
                    "inputs": {
                        "review_verdict": {
                            "decision": "approve",
                            "rationale": "Looks good.",
                            "conditions": ["Ship a short follow-up doc note", ""],
                            "human_recommendation": "Safe to merge.",
                        }
                    },
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        verdict = MEPClient.extract_review_verdict(payload)

        self.assertEqual(
            verdict,
            {
                "decision": "approve",
                "rationale": "Looks good.",
                "conditions": ["Ship a short follow-up doc note"],
                "human_recommendation": "Safe to merge.",
            },
        )

    def test_submit_human_approval_request_dm_builds_structured_payload(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            response = asyncio.run(
                client.submit_human_approval_request_dm(
                    "Two bots approve with conditions; no remaining blocker in code.",
                    "node_master_wu",
                    context_id="pr155-review",
                    reply_to_task_id="task_review_verdict",
                    reply_to_message_id="message_review_verdict",
                    review_decision="approve_with_conditions",
                    blockers=["Need explicit merge confirmation from the human governor"],
                    recommended_next_action="Merge after human approval.",
                    turn_index=3,
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(body["intent"], {"type": "human.approval.request", "priority": "high"})
        self.assertEqual(body["conversation"]["turn_type"], "session_close")
        self.assertEqual(body["conversation"]["turn_index"], 3)
        self.assertEqual(body["conversation"]["context_id"], "pr155-review")
        self.assertEqual(body["task"]["title"], "Human approval request")
        self.assertEqual(
            body["task"]["inputs"]["human_approval_request"],
            {
                "decision_type": "merge_decision",
                "summary": "Two bots approve with conditions; no remaining blocker in code.",
                "review_decision": "approve_with_conditions",
                "blockers": ["Need explicit merge confirmation from the human governor"],
                "recommended_next_action": "Merge after human approval.",
            },
        )
        self.assertEqual(
            body["task"]["inputs"]["governance"],
            {
                "classification": "approval_required",
                "reason": "human approval required for merge_decision",
                "disclosure_scope": ["merge_decision"],
                "redaction_applied": False,
                "approval": {"status": "pending"},
            },
        )
        self.assertEqual(response["context_id"], "pr155-review")

    def test_extract_human_approval_request_reads_structured_payload(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "task": {
                    "instructions": "Human approval request: merge_decision",
                    "inputs": {
                        "human_approval_request": {
                            "decision_type": "merge_decision",
                            "summary": "Need final merge confirmation.",
                            "review_decision": "approve",
                            "blockers": ["Confirm release window", ""],
                            "recommended_next_action": "Merge after approval.",
                        }
                    },
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        request = MEPClient.extract_human_approval_request(payload)

        self.assertEqual(
            request,
            {
                "decision_type": "merge_decision",
                "summary": "Need final merge confirmation.",
                "review_decision": "approve",
                "blockers": ["Confirm release window"],
                "recommended_next_action": "Merge after approval.",
            },
        )

    def test_extract_governance_metadata_reads_structured_payload(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "task": {
                    "instructions": "Share a redacted runtime fact summary.",
                    "inputs": {
                        "governance": {
                            "classification": "approval_required",
                            "reason": "share runtime facts after approval",
                            "disclosure_scope": ["runtime_facts"],
                            "redaction_applied": True,
                            "approval": {
                                "status": "approved",
                                "context_id": "approval-123",
                                "approved_by": "node_human",
                            },
                        }
                    },
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        governance = MEPClient.extract_governance_metadata(payload)

        self.assertEqual(
            governance,
            {
                "classification": "approval_required",
                "reason": "share runtime facts after approval",
                "disclosure_scope": ["runtime_facts"],
                "redaction_applied": True,
                "approval": {
                    "status": "approved",
                    "context_id": "approval-123",
                    "approved_by": "node_human",
                },
            },
        )

    def test_submit_dm_reply_preserves_session_safety_from_inbound_message(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")

            inbound_message = {
                "message_id": "message_review_request",
                "trace_id": "trace-123",
                "timestamp_ms": 1_777_000_000_000,
                "source": {"node_id": "node_reviewer"},
                "intent": {"type": "review.request", "priority": "high"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request", "turn_index": 1},
                "task": {
                    "instructions": "Please review this PR.",
                    "inputs": {
                        "session_safety": {
                            "max_turns": 6,
                            "checkpoint_interval": 3,
                            "started_at_ms": 1_777_000_000_000,
                        }
                    },
                },
            }

            asyncio.run(
                client.submit_dm_reply(
                    "I approve with conditions.",
                    inbound_message,
                    inbound_task_id="task_review_request",
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(
            body["task"]["inputs"]["session_safety"],
            {"max_turns": 6, "checkpoint_interval": 3, "started_at_ms": 1_777_000_000_000},
        )
        self.assertEqual(body["conversation"]["reply_to_task_id"], "task_review_request")
        self.assertEqual(body["conversation"]["reply_to_message_id"], "message_review_request")
        self.assertEqual(body["conversation"]["turn_index"], 2)

    def test_extract_session_safety_reads_structured_payload(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "timestamp_ms": 1_777_000_000_000,
                "task": {
                    "instructions": "Stay in the review thread.",
                    "inputs": {
                        "session_safety": {
                            "max_turns": 5,
                            "max_duration_seconds": 600,
                            "started_at_ms": 1_777_000_000_000,
                        }
                    },
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        session_safety = MEPClient.extract_session_safety(payload)

        self.assertEqual(
            session_safety,
            {"max_turns": 5, "max_duration_seconds": 600, "started_at_ms": 1_777_000_000_000},
        )

    def test_evaluate_interbot_session_safety_requests_checkpoint_at_interval(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "timestamp_ms": 1_777_000_000_000,
                "task": {
                    "instructions": "Stay in the review thread.",
                    "inputs": {"session_safety": {"max_turns": 6, "checkpoint_interval": 3}},
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        evaluation = MEPClient.evaluate_interbot_session_safety(
            payload,
            next_turn_index=3,
            now_ms=1_777_000_100_000,
        )

        self.assertEqual(evaluation["session_safety"], {"max_turns": 6, "checkpoint_interval": 3})
        self.assertTrue(evaluation["should_checkpoint"])
        self.assertFalse(evaluation["should_stop"])
        self.assertEqual(evaluation["violations"], [])

    def test_evaluate_interbot_session_safety_stops_when_limits_are_exceeded(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "timestamp_ms": 1_777_000_000_000,
                "task": {
                    "instructions": "Stay in the review thread.",
                    "inputs": {
                        "session_safety": {"max_turns": 4, "max_duration_seconds": 60, "checkpoint_interval": 2}
                    },
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        evaluation = MEPClient.evaluate_interbot_session_safety(
            payload,
            next_turn_index=5,
            now_ms=1_777_000_070_000,
        )

        self.assertFalse(evaluation["should_checkpoint"])
        self.assertTrue(evaluation["should_stop"])
        self.assertEqual(
            evaluation["violations"],
            ["max_turns_exceeded", "max_duration_exceeded"],
        )

    def test_evaluate_interbot_session_safety_uses_original_session_start_time(self):
        payload = json.dumps(
            {
                "spec_version": "mep.interbot.v1",
                "timestamp_ms": 1_777_000_060_000,
                "task": {
                    "instructions": "Stay in the review thread.",
                    "inputs": {
                        "session_safety": {
                            "max_duration_seconds": 60,
                            "started_at_ms": 1_777_000_000_000,
                        }
                    },
                    "expected_output": {"result_type": "text"},
                },
            }
        )

        evaluation = MEPClient.evaluate_interbot_session_safety(
            payload,
            next_turn_index=2,
            now_ms=1_777_000_070_000,
        )

        self.assertTrue(evaluation["should_stop"])
        self.assertEqual(evaluation["violations"], ["max_duration_exceeded"])

    def test_submit_safe_dm_reply_replies_when_session_is_within_limits(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")
            inbound_message = {
                "message_id": "message_review_request",
                "trace_id": "trace-123",
                "timestamp_ms": 1_777_000_000_000,
                "source": {"node_id": "node_reviewer"},
                "intent": {"type": "review.request", "priority": "high"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request", "turn_index": 1},
                "task": {
                    "instructions": "Please review this PR.",
                    "inputs": {"session_safety": {"max_turns": 6, "checkpoint_interval": 3}},
                },
            }

            response = asyncio.run(
                client.submit_safe_dm_reply(
                    "I approve with conditions.",
                    inbound_message,
                    inbound_task_id="task_review_request",
                    next_turn_index=2,
                    now_ms=1_777_000_010_000,
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(response["reply_action"], "reply")
        self.assertEqual(response["status"], "replied")
        self.assertFalse(response["safety"]["should_stop"])
        self.assertEqual(body["task"]["instructions"], "I approve with conditions.")
        self.assertEqual(body["conversation"]["turn_index"], 2)

    def test_submit_safe_dm_reply_sends_checkpoint_when_interval_is_reached(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.return_value = _FakeResponse()
            client = MEPClient("unused.pem")
            inbound_message = {
                "message_id": "message_review_request",
                "trace_id": "trace-123",
                "timestamp_ms": 1_777_000_000_000,
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "intent": {"type": "review.request", "priority": "high"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request", "turn_index": 2},
                "task": {
                    "instructions": "Please review this PR.",
                    "inputs": {"session_safety": {"max_turns": 6, "checkpoint_interval": 3}},
                },
            }

            response = asyncio.run(
                client.submit_safe_dm_reply(
                    "I approve with conditions.",
                    inbound_message,
                    inbound_task_id="task_review_request",
                    next_turn_index=3,
                    checkpoint_summary="Checkpoint: 3 turns reached.",
                    now_ms=1_777_000_020_000,
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(response["reply_action"], "checkpoint")
        self.assertEqual(response["status"], "checkpointed")
        self.assertTrue(response["safety"]["should_checkpoint"])
        self.assertEqual(body["conversation"]["turn_type"], "checkpoint")
        self.assertEqual(body["conversation"]["turn_index"], 3)
        self.assertEqual(body["task"]["instructions"], "Checkpoint: 3 turns reached.")

    def test_submit_safe_dm_reply_progresses_from_reply_to_checkpoint_to_stop(self):
        with (
            patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()),
            patch("clients.shared.mep_client.requests.Session") as session_cls,
        ):
            session = session_cls.return_value
            session.post.side_effect = [
                _FakeResponse(json_data={"status": "success", "task_id": "task_reply"}),
                _FakeResponse(json_data={"status": "success", "task_id": "task_checkpoint"}),
            ]
            client = MEPClient("unused.pem")
            started_at_ms = 1_777_000_000_000
            inbound_message = {
                "message_id": "message_review_request",
                "trace_id": "trace-123",
                "timestamp_ms": started_at_ms,
                "source": {"node_id": "node_reviewer", "alias": "Reviewer"},
                "intent": {"type": "review.request", "priority": "high"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request", "turn_index": 1},
                "task": {
                    "instructions": "Please review this PR.",
                    "inputs": {
                        "session_safety": {
                            "max_turns": 4,
                            "checkpoint_interval": 3,
                            "max_duration_seconds": 60,
                            "started_at_ms": started_at_ms,
                        }
                    },
                },
            }

            reply_response = asyncio.run(
                client.submit_safe_dm_reply(
                    "I approve with conditions.",
                    inbound_message,
                    inbound_task_id="task_review_request",
                    next_turn_index=2,
                    now_ms=1_777_000_010_000,
                )
            )
            reply_submit_body = json.loads(session.post.call_args_list[0].kwargs["data"])
            reply_message = json.loads(reply_submit_body["task"]["instructions"])

            checkpoint_response = asyncio.run(
                client.submit_safe_dm_reply(
                    "I approve with conditions.",
                    reply_message,
                    inbound_task_id="task_reply",
                    next_turn_index=3,
                    checkpoint_summary="Checkpoint: two review turns completed.",
                    now_ms=1_777_000_020_000,
                )
            )
            checkpoint_submit_body = json.loads(session.post.call_args_list[1].kwargs["data"])
            checkpoint_message = json.loads(checkpoint_submit_body["task"]["instructions"])

            stop_response = asyncio.run(
                client.submit_safe_dm_reply(
                    "I approve with conditions.",
                    checkpoint_message,
                    inbound_task_id="task_checkpoint",
                    next_turn_index=5,
                    now_ms=1_777_000_070_000,
                )
            )

        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(reply_response["reply_action"], "reply")
        self.assertEqual(reply_response["status"], "replied")
        self.assertEqual(
            reply_message["task"]["inputs"]["session_safety"]["started_at_ms"],
            started_at_ms,
        )
        self.assertEqual(reply_message["conversation"]["turn_index"], 2)
        self.assertEqual(checkpoint_response["reply_action"], "checkpoint")
        self.assertEqual(checkpoint_response["status"], "checkpointed")
        self.assertEqual(checkpoint_message["conversation"]["turn_type"], "checkpoint")
        self.assertEqual(checkpoint_message["conversation"]["turn_index"], 3)
        self.assertEqual(
            checkpoint_message["task"]["inputs"]["session_safety"]["started_at_ms"],
            started_at_ms,
        )
        self.assertEqual(stop_response["reply_action"], "stop")
        self.assertEqual(stop_response["status"], "stopped")
        self.assertEqual(stop_response["safety"]["violations"], ["max_turns_exceeded", "max_duration_exceeded"])
        self.assertEqual(stop_response["session_safety"]["started_at_ms"], started_at_ms)

    def test_submit_safe_dm_reply_stops_when_session_limits_are_exceeded(self):
        with patch("clients.shared.mep_client.MEPIdentity", return_value=_FakeIdentity()):
            client = MEPClient("unused.pem")
            inbound_message = {
                "message_id": "message_review_request",
                "trace_id": "trace-123",
                "timestamp_ms": 1_777_000_000_000,
                "source": {"node_id": "node_reviewer"},
                "intent": {"type": "review.request", "priority": "high"},
                "conversation": {"context_id": "pr154-review", "turn_type": "review_request"},
                "task": {
                    "instructions": "Please review this PR.",
                    "inputs": {"session_safety": {"max_turns": 4, "max_duration_seconds": 60}},
                },
            }

            response = asyncio.run(
                client.submit_safe_dm_reply(
                    "I approve with conditions.",
                    inbound_message,
                    inbound_task_id="task_review_request",
                    next_turn_index=5,
                    now_ms=1_777_000_070_000,
                )
            )

        self.assertEqual(response["reply_action"], "stop")
        self.assertEqual(response["status"], "stopped")
        self.assertTrue(response["safety"]["should_stop"])
        self.assertEqual(response["safety"]["violations"], ["max_turns_exceeded", "max_duration_exceeded"])


if __name__ == "__main__":
    unittest.main()
