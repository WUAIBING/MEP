import asyncio
import json
import unittest
from unittest.mock import patch

from clients.shared.mep_client import MEPClient


class _FakeIdentity:
    node_id = "node_consumer"
    pub_pem = "pub"

    def get_auth_headers(self, payload: str) -> dict:
        return {"X-MEP-NodeID": self.node_id, "X-MEP-Signature": "sig"}


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {"status": "success", "task_id": "task_123456"}

    def json(self) -> dict:
        return self._json_data


class TestSharedMEPClient(unittest.TestCase):
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
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(submit_body["routing"], {"target_node_id": "node_reviewer"})
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(body["spec_version"], "mep.interbot.v1")
        self.assertEqual(body["target"]["node_id"], "node_reviewer")
        self.assertEqual(body["conversation"]["context_id"], "pr154-review")
        self.assertEqual(body["conversation"]["reply_to_task_id"], "task_parent")
        self.assertEqual(body["conversation"]["reply_to_message_id"], "message_parent")
        self.assertEqual(body["conversation"]["turn_type"], "review_request")
        self.assertEqual(body["intent"], {"type": "review.request", "priority": "normal"})
        self.assertEqual(body["task"]["instructions"], "Please review PR 154")
        self.assertEqual(body["delivery"], {"reply_mode": "new_dm", "settlement_mode": "task_result"})
        self.assertEqual(response["context_id"], "pr154-review")
        self.assertTrue(response["message_id"])
        self.assertTrue(response["trace_id"])

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
                )
            )

        submit_body = json.loads(session.post.call_args.kwargs["data"])
        body = json.loads(submit_body["task"]["instructions"])
        self.assertEqual(body["intent"], {"type": "review.response", "priority": "normal"})
        self.assertEqual(body["conversation"]["turn_type"], "approval")
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


if __name__ == "__main__":
    unittest.main()
