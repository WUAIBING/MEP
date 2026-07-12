import asyncio
import json
import unittest
from unittest.mock import patch

from node.client import ChronosNode


class _FakeIdentity:
    node_id = "node_consumer"
    pub_pem = "pub"

    def get_auth_headers(self, payload: str) -> dict:
        return {"X-MEP-NodeID": self.node_id, "X-MEP-Signature": "sig"}


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {"status": "success", "task_id": "task_123456"}
        self.text = text

    def json(self) -> dict:
        return self._json_data


class TestChronosNodeSubmitTask(unittest.TestCase):
    def test_submit_task_uses_spec_shaped_envelope(self):
        with (
            patch("node.client.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.client.ReputationManager"),
            patch("node.client.requests.post", return_value=_FakeResponse()) as post_mock,
        ):
            node = ChronosNode("unused.pem", hub_url="http://hub")
            asyncio.run(
                node.submit_task(
                    "summarize this",
                    bounty=1.5,
                    target_node="node_provider",
                    target_capability="text",
                )
            )

        body = json.loads(post_mock.call_args.kwargs["data"])
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
        self.assertEqual(
            body["routing"],
            {"target_node_id": "node_provider", "target_capability": "text"},
        )

    def test_submit_task_reports_200_error_body_without_task_id(self):
        response = _FakeResponse(200, {"status": "error", "detail": "Target node not currently connected to Hub"})
        with (
            patch("node.client.MEPIdentity", return_value=_FakeIdentity()),
            patch("node.client.ReputationManager"),
            patch("node.client.requests.post", return_value=response),
            patch("builtins.print") as print_mock,
        ):
            node = ChronosNode("unused.pem", hub_url="http://hub")
            asyncio.run(node.submit_task("hello", bounty=0.0, target_node="node_offline"))

        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Failed to submit task", printed)
        self.assertIn("Target node not currently connected", printed)


if __name__ == "__main__":
    unittest.main()
