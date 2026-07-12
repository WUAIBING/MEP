import os
import sys
import json
import tempfile
import types
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "node"))

# The CLI provider can run standalone with optional websocket/aiohttp runtime
# dependencies. These unit tests only exercise local safety helpers.
sys.modules.setdefault(
    "websockets",
    types.SimpleNamespace(exceptions=types.SimpleNamespace(ConnectionClosed=Exception), connect=None),
)
sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(ClientSession=None),
)

from mep_cli_provider import MEPCLIProvider  # noqa: E402


class TestMEPCLIProviderSafety(unittest.IsolatedAsyncioTestCase):
    def test_build_agent_argv_keeps_payload_as_single_arg(self):
        payload = "hello; touch /tmp/pwned && echo bad"
        argv = MEPCLIProvider._build_agent_argv("python -m local_agent", payload)
        self.assertEqual(argv[-1], payload)
        self.assertNotIn(";", argv[:-1])

    def test_build_agent_argv_replaces_payload_placeholder_without_shell(self):
        payload = "$(touch /tmp/pwned)"
        argv = MEPCLIProvider._build_agent_argv("agent --task {payload}", payload)
        self.assertEqual(argv, ["agent", "--task", payload])

    async def test_process_task_rejects_executable_task_when_disabled(self):
        provider = MEPCLIProvider.__new__(MEPCLIProvider)
        provider.workspace_dir = tempfile.mkdtemp()
        provider.allow_execution = False
        provider.upload_code = False
        provider.max_code_chars = 12000
        provider.node_id = "node_test"
        provider.identity = types.SimpleNamespace(get_auth_headers=lambda payload: {"X-Test-Payload": payload})

        calls = []

        async def fake_post(*args, **kwargs):
            calls.append((args, kwargs))
            return types.SimpleNamespace(status_code=200, text="ok")

        provider._post_with_retry = fake_post
        await provider.process_task(
            {
                "id": "task_exec_disabled",
                "payload": "import os\nprint(os.listdir('.'))",
                "bounty": 1.0,
                "consumer_id": "node_consumer",
            }
        )
        self.assertEqual(len(calls), 1)
        url = calls[0][0][0]
        payload = json.loads(calls[0][1]["payload_str"])
        self.assertTrue(url.endswith("/tasks/reject"))
        self.assertEqual(payload["task_id"], "task_exec_disabled")
        self.assertEqual(payload["provider_id"], "node_test")
        self.assertEqual(payload["reason"], "execution_disabled")


if __name__ == "__main__":
    unittest.main()
