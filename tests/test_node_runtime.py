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


class TestNodeRuntimeHelpers(unittest.TestCase):
    def test_status_badges_all_ok(self):
        badges = mep_runtime._status_badges(
            {"registered": True, "ws_connected": True, "last_heartbeat": 123.0, "availability": "online"},
            ai_ready=True,
        )
        self.assertTrue(all(badges.values()))

    def test_build_doctor_snapshot_includes_heartbeat_delta(self):
        with patch("node.mep_runtime.time.time", return_value=1000.0):
            snapshot = mep_runtime._build_doctor_snapshot(
                node_id="node_1",
                diag={"registered": True, "ws_connected": False, "last_heartbeat": 900.0},
                auth_status="ok",
                dm_status="ok",
                listener_contract_ok=True,
                ai_configured=True,
                clock_skew_seconds=2.5,
            )
        self.assertEqual(snapshot["node_id"], "node_1")
        self.assertEqual(snapshot["heartbeat_seconds_ago"], 100.0)
        self.assertFalse(snapshot["ws_connected"])
        self.assertEqual(snapshot["clock_skew_seconds"], 2.5)

    def test_mock_adapter_is_deterministic(self):
        adapter = mep_runtime.MockAdapter()
        out = adapter.generate_reply("hello world", {"id": "123456789"})
        self.assertIn("MOCK_ADAPTER_OK", out)
        self.assertIn("task=12345678", out)
        self.assertIn("summary=hello world", out)


class TestNodeRuntimeCommands(unittest.TestCase):
    def test_status_command_success(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            require_online=False,
        )
        diag = {"registered": True, "ws_connected": True, "last_heartbeat": 100.0, "availability": "online"}
        with (
            patch("node.mep_runtime.MEPIdentity") as identity_cls,
            patch("node.mep_runtime.requests.request", return_value=_FakeResponse(200, diag)),
        ):
            identity_cls.return_value.node_id = "node_test"
            code = mep_runtime.cmd_status(args)
        self.assertEqual(code, 0)

    def test_doctor_command_posts_snapshot(self):
        args = argparse.Namespace(
            hub_url="http://hub",
            key_path="C:/tmp/test_key.pem",
            adapter="mock",
            auth_status="ok",
            dm_status="pending",
            listener_contract_ok=False,
            clock_skew_seconds=None,
        )
        diag = {"registered": True, "ws_connected": False, "last_heartbeat": 100.0, "availability": "offline"}
        diagnosis = {
            "root_cause": "dm_pending_target_offline_or_route_issue",
            "severity": "medium",
            "fix_steps": [],
            "copy_paste_commands": [],
            "telemetry": {"total_requests": 1, "root_cause_count": 1},
        }
        with (
            patch("node.mep_runtime.MEPIdentity") as identity_cls,
            patch("node.mep_runtime.requests.request") as request_mock,
        ):
            identity_cls.return_value.node_id = "node_test"
            request_mock.side_effect = [
                _FakeResponse(200, diag),
                _FakeResponse(200, diagnosis),
            ]
            code = mep_runtime.cmd_doctor(args)
            sent_payload = request_mock.call_args_list[1].kwargs["json"]

        self.assertEqual(code, 0)
        self.assertEqual(sent_payload["node_id"], "node_test")
        self.assertFalse(sent_payload["listener_contract_ok"])
        self.assertEqual(sent_payload["dm_status"], "pending")


if __name__ == "__main__":
    unittest.main()
