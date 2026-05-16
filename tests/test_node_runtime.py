import argparse
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
