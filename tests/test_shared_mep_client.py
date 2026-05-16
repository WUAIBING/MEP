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


if __name__ == "__main__":
    unittest.main()
