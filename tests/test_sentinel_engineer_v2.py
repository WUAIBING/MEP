"""
Comprehensive tests for SentinelEngineer v2.
Tests the core autonomous execution loop WITHOUT needing a running Hub or real LLM.
"""
import json
import sys
import os
import time
import tempfile
import unittest
from unittest.mock import MagicMock
from pathlib import Path

# Add node dir to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "node"))

# ---- Test Output Parser ----
from sentinel_engineer_v2 import (
    parse_llm_response, ExecResult, CodeExecutor,
    MultiBrain, CircuitBreaker, ProviderStatus, CONFIG
)


class TestParseLLMResponse(unittest.TestCase):
    """Test the resilient JSON parser."""

    def test_clean_json(self):
        raw = '{"thought": "hello", "code": null, "done": true, "final_answer": "42"}'
        action = parse_llm_response(raw)
        self.assertTrue(action.done)
        self.assertEqual(action.final_answer, "42")
        self.assertIsNone(action.code)

    def test_json_with_code(self):
        raw = json.dumps({
            "thought": "compute sum",
            "code": "print(1+1)",
            "done": False,
            "final_answer": ""
        })
        action = parse_llm_response(raw)
        self.assertFalse(action.done)
        self.assertEqual(action.code, "print(1+1)")

    def test_markdown_fence(self):
        raw = 'Here is the result:\n```json\n{"thought": "done", "code": null, "done": true, "final_answer": "result"}\n```'
        action = parse_llm_response(raw)
        self.assertTrue(action.done)
        self.assertEqual(action.final_answer, "result")

    def test_markdown_fence_no_lang(self):
        raw = '```\n{"thought": "ok", "done": true, "final_answer": "yes"}\n```'
        action = parse_llm_response(raw)
        self.assertTrue(action.done)

    def test_json_embedded_in_text(self):
        raw = 'Let me think... {"thought": "aha", "code": "x=1", "done": false, "final_answer": ""} That should work.'
        action = parse_llm_response(raw)
        self.assertEqual(action.code, "x=1")

    def test_trailing_commas(self):
        raw = '{"thought": "test", "code": null, "done": true, "final_answer": "ok",}'
        action = parse_llm_response(raw)
        self.assertTrue(action.done)

    def test_single_quotes(self):
        raw = "{'thought': 'test', 'done': true, 'final_answer': 'ok'}"
        action = parse_llm_response(raw)
        self.assertTrue(action.done)

    def test_garbage_input(self):
        raw = "I can't figure this out, sorry!"
        action = parse_llm_response(raw)
        # Should degrade to final answer
        self.assertTrue(action.done)
        self.assertIn("sorry", action.final_answer)

    def test_empty_string(self):
        action = parse_llm_response("")
        self.assertTrue(action.done)

    def test_missing_fields(self):
        raw = '{"thought": "something"}'
        action = parse_llm_response(raw)
        self.assertFalse(action.done)
        self.assertIsNone(action.code)


class TestCircuitBreaker(unittest.TestCase):
    """Test circuit breaker behavior."""

    def test_starts_available(self):
        cb = CircuitBreaker()
        self.assertTrue(cb.is_available())

    def test_trips_after_threshold(self):
        cb = CircuitBreaker()
        for _ in range(CONFIG.circuit_breaker_threshold):
            cb.record_failure()
        self.assertFalse(cb.is_available())

    def test_success_resets(self):
        cb = CircuitBreaker()
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        self.assertEqual(cb.failure_count, 0)
        self.assertTrue(cb.is_available())

    def test_cooldown_recovery(self):
        cb = CircuitBreaker()
        for _ in range(CONFIG.circuit_breaker_threshold):
            cb.record_failure()
        self.assertFalse(cb.is_available())
        # Simulate cooldown elapsed
        cb.last_failure_time = 0
        self.assertTrue(cb.is_available())


class TestCodeExecutor(unittest.TestCase):
    """Test sandboxed code execution."""

    def setUp(self):
        self.executor = CodeExecutor()

    def test_python_hello(self):
        result = self.executor.execute('print("hello")', "python")
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)

    def test_python_error(self):
        result = self.executor.execute('1/0', "python")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ZeroDivision", result.stderr)

    @unittest.skipIf(sys.platform == "win32", "bash not available on Windows")
    def test_bash_echo(self):
        result = self.executor.execute('echo "test"', "bash")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test", result.stdout)

    def test_unsupported_language(self):
        result = self.executor.execute('alert("x")', "javascript")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported", result.stderr)

    def test_sandbox_cleanup(self):
        """Verify temp directories are cleaned up."""
        sandbox_base = Path(tempfile.gettempdir())
        before = set(sandbox_base.iterdir())
        self.executor.execute('print("cleanup test")', "python")
        after = set(sandbox_base.iterdir())
        # Our se_* dirs should be gone
        new_dirs = after - before
        se_dirs = [d for d in new_dirs if d.name.startswith("se_")]
        self.assertEqual(len(se_dirs), 0, f"Leaked sandbox dirs: {se_dirs}")

    def test_timeout(self):
        """Test that long-running code is killed."""
        old_timeout = CONFIG.code_timeout
        CONFIG.code_timeout = 2
        try:
            result = self.executor.execute('import time; time.sleep(60)', "python")
            self.assertTrue(result.timed_out)
            self.assertEqual(result.returncode, 124)
        finally:
            CONFIG.code_timeout = old_timeout

    def test_file_isolation(self):
        """Code can't write outside sandbox."""
        leak_path = os.path.join(tempfile.gettempdir(), "se_test_leak.txt")
        self.executor.execute(
            f'with open("{leak_path.replace(chr(92), "/")}", "w") as f: f.write("leak")',
            "python"
        )
        # File might be created in sandbox, but not at /tmp/se_test_leak.txt
        # (actually /tmp is writable, so this tests that sandbox CWD doesn't leak)
        # Better test: verify we're running in a temp dir
        result2 = self.executor.execute('import os; print(os.getcwd())', "python")
        self.assertIn("se_", result2.stdout)  # CWD should be in sandbox


class TestExecResult(unittest.TestCase):

    def test_summary_truncation(self):
        result = ExecResult(stdout="x" * 20000, stderr="", returncode=0)
        summary = result.summary(max_chars=100)
        self.assertIn("x" * 100, summary)
        self.assertNotIn("x" * 101, summary)


class TestMultiBrainMocked(unittest.TestCase):
    """Test MultiBrain with mocked providers."""

    def _make_brain(self, providers):
        """Helper: create a MultiBrain with pre-built provider list."""
        brain = MultiBrain.__new__(MultiBrain)
        brain.history = []
        brain.providers = [(p, CircuitBreaker()) for p in providers]
        return brain

    def test_fallback_on_failure(self):
        gemini = MagicMock()
        gemini.name = "gemini"
        gemini.call.side_effect = RuntimeError("Gemini down")

        deepseek = MagicMock()
        deepseek.name = "deepseek"
        deepseek.call.return_value = "success from deepseek"

        brain = self._make_brain([gemini, deepseek])
        result = brain.generate("test")
        self.assertEqual(result, "success from deepseek")
        gemini.call.assert_called_once()
        deepseek.call.assert_called_once()

    def test_all_fail_raises(self):
        gemini = MagicMock()
        gemini.name = "gemini"
        gemini.call.side_effect = RuntimeError("down")

        deepseek = MagicMock()
        deepseek.name = "deepseek"
        deepseek.call.side_effect = RuntimeError("also down")

        brain = self._make_brain([gemini, deepseek])
        with self.assertRaises(RuntimeError):
            brain.generate("test")

    def test_circuit_breaker_skips_tripped(self):
        failing = MagicMock()
        failing.name = "failing"
        failing.call.side_effect = RuntimeError("boom")

        working = MagicMock()
        working.name = "working"
        working.call.return_value = "ok"

        brain = self._make_brain([failing, working])
        # Trip the first provider
        cb = brain.providers[0][1]
        cb.failure_count = 3
        cb.status = ProviderStatus.TRIPPED
        cb.last_failure_time = time.time()  # Just failed, cooldown not elapsed

        result = brain.generate("test")
        self.assertEqual(result, "ok")
        failing.call.assert_not_called()  # Skipped due to circuit breaker


if __name__ == "__main__":
    unittest.main()
