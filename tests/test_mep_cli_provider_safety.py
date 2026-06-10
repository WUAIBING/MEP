import os
import sys
import tempfile
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "node"))

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

    async def test_process_task_skips_executable_task_when_disabled(self):
        provider = MEPCLIProvider.__new__(MEPCLIProvider)
        provider.workspace_dir = tempfile.mkdtemp()
        provider.allow_execution = False
        provider.upload_code = False
        provider.max_code_chars = 12000
        provider.node_id = "node_test"

        calls = []

        async def fake_post(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        provider._post_with_retry = fake_post
        await provider.process_task(
            {
                "id": "task_exec_disabled",
                "payload": "import os\nprint(os.listdir('.'))",
                "bounty": 1.0,
                "consumer_id": "node_consumer",
            }
        )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
