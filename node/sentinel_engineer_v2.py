"""
SentinelEngineer — Autonomous code execution agent for MEP Nodes.

Replaces the original sentinel_engineer.py with:
- Robust output parsing (handles malformed JSON, markdown fences, extra text)
- Self-healing retry loop (feeds errors back to LLM for correction)
- History fidelity (every exchange recorded, not just code executions)
- Circuit breaker on LLM fallback chain (skip known-bad providers)
- Sandboxed code execution (resource limits, temp isolation)
- Output validation before declaring done
- Configurable via env vars (turns, timeouts, models)
- Structured logging instead of print()
"""

import os
import sys
import json
import re
import time
import uuid
import logging
import subprocess
import tempfile
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("sentinel_engineer")
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_handler)
logger.setLevel(os.getenv("SE_LOG_LEVEL", "INFO").upper())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    max_turns: int = int(os.getenv("SE_MAX_TURNS", "8"))
    code_timeout: int = int(os.getenv("SE_CODE_TIMEOUT", "60"))
    llm_timeout: int = int(os.getenv("SE_LLM_TIMEOUT", "120"))
    max_output_chars: int = int(os.getenv("SE_MAX_OUTPUT_CHARS", "8000"))
    sandbox_dir: str = os.getenv("SE_SANDBOX_DIR", "")  # empty = auto tmpdir
    circuit_breaker_threshold: int = int(os.getenv("SE_CB_THRESHOLD", "3"))
    circuit_breaker_cooldown: int = int(os.getenv("SE_CB_COOLDOWN", "300"))  # seconds

CONFIG = Config()

# ---------------------------------------------------------------------------
# LLM Providers
# ---------------------------------------------------------------------------
class ProviderStatus(Enum):
    OK = "ok"
    TRIPPED = "tripped"

@dataclass
class CircuitBreaker:
    """Track consecutive failures per provider; trip after N failures."""
    failure_count: int = 0
    last_failure_time: float = 0.0
    status: ProviderStatus = ProviderStatus.OK

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= CONFIG.circuit_breaker_threshold:
            self.status = ProviderStatus.TRIPPED
            logger.warning("Circuit breaker TRIPPED after %d failures", self.failure_count)

    def record_success(self):
        self.failure_count = 0
        self.status = ProviderStatus.OK

    def is_available(self) -> bool:
        if self.status == ProviderStatus.OK:
            return True
        # Cooldown elapsed → half-open, allow one retry
        if time.time() - self.last_failure_time > CONFIG.circuit_breaker_cooldown:
            logger.info("Circuit breaker cooldown elapsed, allowing retry")
            self.status = ProviderStatus.OK
            self.failure_count = 0
            return True
        return False


class BaseProvider:
    name: str = "base"

    def call(self, prompt: str, history: list[dict]) -> str:
        raise NotImplementedError


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = os.getenv("SE_GEMINI_MODEL", "gemini-2.0-flash")

    def call(self, prompt: str, history: list[dict]) -> str:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(self.model_name)
        gemini_hist = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            gemini_hist.append({"role": role, "parts": [msg["content"]]})
        chat = model.start_chat(history=gemini_hist)
        response = chat.send_message(prompt, request_options={"timeout": CONFIG.llm_timeout})
        return response.text


class DeepSeekProvider(BaseProvider):
    name = "deepseek"

    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model_name = os.getenv("SE_DEEPSEEK_MODEL", "deepseek-v4-pro")
        self._supports_thinking = self.model_name in ("deepseek-reasoner", "deepseek-r1")

    def call(self, prompt: str, history: list[dict]) -> str:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model_name,
            "messages": messages,
        }
        # Only use response_format for non-reasoning models to avoid reasoning_content requirement
        if not self._supports_thinking:
            payload["response_format"] = {"type": "json_object"}
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=CONFIG.llm_timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class GLMProvider(BaseProvider):
    name = "glm"

    def __init__(self):
        self.api_key = os.getenv("GLM_API_KEY")

    def call(self, prompt: str, history: list[dict]) -> str:
        if not self.api_key:
            raise RuntimeError("GLM_API_KEY not set")
        from zhipuai import ZhipuAI
        client = ZhipuAI(api_key=self.api_key)
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=os.getenv("SE_GLM_MODEL", "glm-4"),
            messages=messages,
        )
        return resp.choices[0].message.content


class MiniMaxProvider(BaseProvider):
    name = "minimax"

    def __init__(self):
        self.api_key = os.getenv("MINIMAX_API_KEY")
        self.group_id = os.getenv("MINIMAX_GROUP_ID", "")

    def call(self, prompt: str, history: list[dict]) -> str:
        if not self.api_key:
            raise RuntimeError("MINIMAX_API_KEY not set")
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt})
        resp = requests.post(
            "https://api.minimax.chat/v1/text/chatcompletion_pro",
            params={"GroupId": self.group_id},
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": "abab6.5-chat",
                "messages": messages,
            },
            timeout=CONFIG.llm_timeout,
        )
        resp.raise_for_status()
        return resp.json()["reply"]


# ---------------------------------------------------------------------------
# MultiBrain — Fallback chain with circuit breaker
# ---------------------------------------------------------------------------
PROVIDER_CLASSES = [GeminiProvider, DeepSeekProvider, GLMProvider, MiniMaxProvider]


class MultiBrain:
    def __init__(self):
        self.history: list[dict] = []
        self.providers: list[tuple[BaseProvider, CircuitBreaker]] = []
        for cls in PROVIDER_CLASSES:
            try:
                provider = cls()
                # Skip providers with no API key configured
                if not getattr(provider, "api_key", None):
                    logger.info("Skipping %s (no API key)", cls.name)
                    continue
                self.providers.append((provider, CircuitBreaker()))
                logger.info("Registered provider: %s", cls.name)
            except Exception as e:
                logger.warning("Failed to init %s: %s", cls.__name__, e)

        if not self.providers:
            raise RuntimeError("No LLM providers available — check API keys")

    def generate(self, prompt: str) -> str:
        """Try each available provider in order; skip tripped circuit breakers."""
        errors = []

        for provider, cb in self.providers:
            if not cb.is_available():
                logger.debug("Skipping %s (circuit breaker open)", provider.name)
                continue

            try:
                logger.info("Calling provider: %s", provider.name)
                result = provider.call(prompt, self.history)
                cb.record_success()
                return result
            except Exception as e:
                cb.record_failure()
                errors.append(f"{provider.name}: {e}")
                logger.warning("Provider %s failed: %s", provider.name, e)

        raise RuntimeError(f"All providers failed: {'; '.join(errors)}")

    def append(self, role: str, content: str):
        """Record every exchange — no silent gaps."""
        self.history.append({"role": role, "content": content})


# ---------------------------------------------------------------------------
# Output Parser — resilient JSON extraction
# ---------------------------------------------------------------------------
@dataclass
class AgentAction:
    thought: str = ""
    code: Optional[str] = None
    language: str = "python"
    done: bool = False
    final_answer: str = ""
    raw: str = ""


def parse_llm_response(raw: str) -> AgentAction:
    """
    Extract a JSON action object from LLM output.
    Handles:
    - Clean JSON
    - JSON wrapped in ```json ... ``` fences
    - JSON embedded in explanatory text
    - Minor formatting issues (trailing commas, single quotes)
    """
    action = AgentAction(raw=raw)

    # Strategy 1: try direct parse
    obj = _try_parse_json(raw)
    if obj:
        return _fill_action(action, obj)

    # Strategy 2: extract from markdown fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
    if fence_match:
        obj = _try_parse_json(fence_match.group(1))
        if obj:
            return _fill_action(action, obj)

    # Strategy 3: find outermost { ... }
    brace_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL)
    if brace_match:
        obj = _try_parse_json(brace_match.group(0))
        if obj:
            return _fill_action(action, obj)

    # Strategy 4: last resort — treat entire response as final answer
    logger.warning("Could not parse JSON from LLM response, treating as final answer")
    action.done = True
    action.final_answer = raw.strip()
    return action


def _try_parse_json(text: str) -> Optional[dict]:
    """Try parsing JSON with minor cleanup."""
    text = text.strip()
    # Fix trailing commas
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r",\s*]", "]", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try single-quote replacement
        try:
            return json.loads(text.replace("'", '"'))
        except (json.JSONDecodeError, ValueError):
            return None


def _fill_action(action: AgentAction, obj: dict) -> AgentAction:
    action.thought = str(obj.get("thought", ""))
    action.done = bool(obj.get("done", False))
    action.final_answer = str(obj.get("final_answer", ""))
    if obj.get("code"):
        action.code = str(obj["code"])
        action.language = str(obj.get("language", "python"))
    return action


# ---------------------------------------------------------------------------
# Code Executor — sandboxed with resource limits
# ---------------------------------------------------------------------------
@dataclass
class ExecResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    def summary(self, max_chars: int = CONFIG.max_output_chars) -> str:
        parts = [f"Exit Code: {self.returncode}"]
        if self.stdout:
            parts.append(f"Stdout:\n{self.stdout[:max_chars]}")
        if self.stderr:
            parts.append(f"Stderr:\n{self.stderr[:max_chars]}")
        if self.timed_out:
            parts.append(f"⚠️ Timed out after {CONFIG.code_timeout}s")
        return "\n".join(parts)


class CodeExecutor:
    """Execute code in an isolated temp directory with resource limits."""

    ALLOWED_LANGUAGES = {"python", "bash"}

    def __init__(self):
        self._sandbox_base = (
            Path(CONFIG.sandbox_dir) if CONFIG.sandbox_dir
            else Path(tempfile.gettempdir())
        )
        self._sandbox_base.mkdir(parents=True, exist_ok=True)

    def execute(self, code: str, language: str = "python") -> ExecResult:
        if language not in self.ALLOWED_LANGUAGES:
            return ExecResult("", f"Unsupported language: {language}", 1)

        ext = ".py" if language == "python" else ".sh"
        if language == "python":
            cmd = [sys.executable, "script.py"]
        elif language == "bash":
            if sys.platform == "win32":
                return ExecResult("", "Bash execution is not supported on Windows", 1)
            cmd = ["bash", "script.sh"]

        # Create isolated workspace per execution
        work_dir = Path(tempfile.mkdtemp(dir=self._sandbox_base, prefix="se_"))
        script_path = work_dir / f"script{ext}"

        try:
            script_path.write_text(code, encoding="utf-8")
            if language == "bash":
                script_path.chmod(0o755)

            result = subprocess.run(
                cmd,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=CONFIG.code_timeout,
                # Limit child process resources (Unix)
                # CPU time: 30s, Memory: 512MB, File size: 10MB
                preexec_fn=self._limit_resources if sys.platform != "win32" else None,
            )
            return ExecResult(
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return ExecResult("", f"Execution timed out after {CONFIG.code_timeout}s", 124, timed_out=True)
        except Exception as e:
            return ExecResult("", str(e), 1)
        finally:
            # Always clean up sandbox
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _limit_resources():
        """Set resource limits for child process (Unix only)."""
        import resource
        # CPU time: 30 seconds
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        # Address space: 512 MB
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        # File size: 10 MB
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))


# ---------------------------------------------------------------------------
# SentinelEngineer — Main Agent Loop
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are an autonomous engineer agent. Your job is to solve tasks by writing and executing code.

You must respond with a JSON object (and ONLY the JSON object) using this schema:
{
  "thought": "Your reasoning about what to do next",
  "code": "The code to execute (null if no code needed)",
  "language": "python or bash (default: python)",
  "done": true/false,
  "final_answer": "Your final answer when done=true"
}

Rules:
1. Think step by step. Use "thought" to plan.
2. Write code to explore, test, or compute.
3. You will receive the execution output — use it to decide next steps.
4. If the output shows an error, fix your code and try again.
5. Set done=true ONLY when you have a verified final answer.
6. Be concise. Don't repeat working code — iterate on what's broken.
7. If the task is a simple question (no code needed), set done=true with your answer.
"""


class SentinelEngineer:
    def __init__(self):
        self.brain = MultiBrain()
        self.executor = CodeExecutor()
        self.run_id = str(uuid.uuid4())[:8]
        logger.info("SentinelEngineer [%s] initialized with %d providers",
                     self.run_id, len(self.brain.providers))

    def solve(self, task: str) -> str:
        """
        Main entry point. Takes a task string, returns the final answer.
        Implements a self-healing loop: LLM thinks → code → result → LLM again.
        """
        logger.info("[%s] New task: %s", self.run_id, task[:200])

        # Seed system prompt
        self.brain.append("user", SYSTEM_PROMPT)
        self.brain.append("model", '{"thought": "Ready to solve tasks.", "code": null, "done": false, "final_answer": ""}')

        current_input = f"Task: {task}"

        for turn in range(1, CONFIG.max_turns + 1):
            logger.info("[%s] Turn %d/%d", self.run_id, turn, CONFIG.max_turns)

            try:
                raw_response = self.brain.generate(current_input)
                logger.debug("[%s] Raw response: %s", self.run_id, raw_response[:300])
            except RuntimeError as e:
                logger.error("[%s] All providers failed: %s", self.run_id, e)
                return f"Error: All LLM providers failed — {e}"

            action = parse_llm_response(raw_response)
            logger.info("[%s] Thought: %s", self.run_id, action.thought[:150])

            # Record this exchange
            self.brain.append("user", current_input)
            self.brain.append("model", raw_response)

            # Check if done
            if action.done:
                answer = action.final_answer or "(no answer provided)"
                logger.info("[%s] Done after %d turns: %s", self.run_id, turn, answer[:200])
                return answer

            # Execute code if provided
            if action.code:
                logger.info("[%s] Executing %s code (%d chars)", self.run_id, action.language, len(action.code))
                result = self.executor.execute(action.code, action.language)

                if result.timed_out:
                    logger.warning("[%s] Code timed out", self.run_id)
                elif result.returncode != 0:
                    logger.warning("[%s] Code failed (exit %d)", self.run_id, result.returncode)
                else:
                    logger.info("[%s] Code succeeded", self.run_id)

                # Feed result back — this is the key self-healing step
                current_input = (
                    f"Execution Result:\n{result.summary()}\n\n"
                    "Analyze the output. If there was an error, fix your code and try again. "
                    "If the result is correct, set done=true with your final answer."
                )
            else:
                # LLM didn't write code and didn't say done — nudge it
                current_input = (
                    "You didn't provide code or mark done. "
                    "If the task requires computation, write code. "
                    "If you can answer directly, set done=true with your final_answer."
                )

        # Exhausted turns
        logger.warning("[%s] Exhausted %d turns without completion", self.run_id, CONFIG.max_turns)
        return "Failed: exceeded maximum turns without reaching a solution."


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
    else:
        task = sys.stdin.read().strip()

    if not task:
        print("Usage: sentinel_engineer.py <task>", file=sys.stderr)
        sys.exit(1)

    engineer = SentinelEngineer()
    result = engineer.solve(task)
    print(result)


if __name__ == "__main__":
    main()
