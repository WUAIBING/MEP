"""
tests/test_onboard_bot.py — Unit tests for hub/onboard_bot.py

Run with: pytest tests/test_onboard_bot.py -v
"""

import pytest
import requests_mock
from hub.onboard_bot import (
    OnboardBot,
    OllamaClient,
    _get_faq_answer,
    SYSTEM_PROMPT,
    FAQ_ANSWERS,
    _NODE_ID,
    _ALIAS,
)


# ---------------------------------------------------------------------------
# OllamaClient tests
# ---------------------------------------------------------------------------
class TestOllamaClient:
    def test_build_payload_includes_system_prompt(self):
        client = OllamaClient(base_url="http://localhost:11434", model="llama3.2:1b")
        payload = client._build_payload("How do I register?")
        assert payload["model"] == "llama3.2:1b"
        assert payload["stream"] is False
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert SYSTEM_PROMPT in payload["messages"][0]["content"]
        assert payload["messages"][1]["role"] == "user"
        assert payload["messages"][1]["content"] == "How do I register?"

    def test_build_payload_uses_configured_model(self):
        client = OllamaClient(model="phi:2.7b")
        payload = client._build_payload("test")
        assert payload["model"] == "phi:2.7b"

    def test_build_payload_temperature_and_tokens(self):
        client = OllamaClient()
        payload = client._build_payload("short answer")
        assert payload["options"]["temperature"] == 0.3
        assert payload["options"]["num_predict"] == 300


class TestOllamaClientCall:
    def test_call_success(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            json={"message": {"role": "assistant", "content": "Register via POST /register"}},
            status_code=200,
        )
        client = OllamaClient()
        text, latency = client._call(client._build_payload("How do I register?"))
        assert text == "Register via POST /register"
        assert latency >= 0

    def test_call_timeout(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            exc=requests.exceptions.Timeout("timed out"),
        )
        client = OllamaClient(timeout=2)
        with pytest.raises(requests.exceptions.Timeout):
            client._call(client._build_payload("ping"))

    def test_call_http_error(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            status_code=500,
            text="Internal Server Error",
        )
        client = OllamaClient()
        with pytest.raises(requests.HTTPError):
            client._call(client._build_payload("ping"))


class TestOllamaClientAsk:
    @pytest.mark.asyncio
    async def test_ask_success(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            json={"message": {"role": "assistant", "content": "Try POST /register"}},
            status_code=200,
        )
        client = OllamaClient()
        answer, latency, used_ollama = await client.ask("How do I register?")
        assert used_ollama is True
        assert answer == "Try POST /register"
        assert latency > 0

    @pytest.mark.asyncio
    async def test_ask_timeout_falls_back_to_faq(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            exc=requests.exceptions.Timeout("timed out"),
        )
        client = OllamaClient(timeout=2)
        answer, latency, used_ollama = await client.ask("I'm getting 401 errors")
        assert used_ollama is False
        assert "401" in answer or "FAQ" in answer or "timed out" in answer
        assert latency == 2.0  # timeout value

    @pytest.mark.asyncio
    async def test_ask_connection_error_falls_back_to_faq(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            exc=requests.exceptions.ConnectionError("Connection refused"),
        )
        client = OllamaClient()
        answer, latency, used_ollama = await client.ask("my websocket won't connect")
        assert used_ollama is False
        assert "faq" not in answer.lower() or "unavailable" in answer.lower()

    @pytest.mark.asyncio
    async def test_ask_generic_error(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            status_code=500,
            text="boom",
        )
        client = OllamaClient()
        answer, latency, used_ollama = await client.ask("hello")
        assert used_ollama is False
        assert latency == 0.0


# ---------------------------------------------------------------------------
# FAQ tests
# ---------------------------------------------------------------------------
class TestFAQ:
    def test_faq_answer_401(self):
        assert _get_faq_answer("I'm getting 401 errors") is not None
        assert "401" in _get_faq_answer("I'm getting 401 errors")

    def test_faq_answer_websocket(self):
        assert _get_faq_answer("my websocket won't connect") is not None
        assert "signature" in _get_faq_answer("my websocket won't connect").lower()

    def test_faq_answer_register(self):
        assert _get_faq_answer("how do i register?") is not None
        assert "register" in _get_faq_answer("how do i register?").lower()

    def test_faq_answer_pending_dms(self):
        assert _get_faq_answer("my dms are pending") is not None

    def test_faq_answer_unknown_topic_returns_none(self):
        assert _get_faq_answer("what is the meaning of life") is None


# ---------------------------------------------------------------------------
# OnboardBot tests
# ---------------------------------------------------------------------------
class TestOnboardBotAsk:
    @pytest.mark.asyncio
    async def test_ask_returns_expected_shape(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            json={"message": {"role": "assistant", "content": "Answer text"}},
            status_code=200,
        )
        bot = OnboardBot(hub_url="http://localhost:8000")
        result = await bot.ask("How do I register?", node_id="node_abc123")
        assert "answer" in result
        assert "latency_s" in result
        assert "source" in result
        assert result["node_id"] == "node_abc123"
        assert result["source"] == "ollama"

    @pytest.mark.asyncio
    async def test_ask_updates_stats(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            json={"message": {"role": "assistant", "content": "ok"}},
            status_code=200,
        )
        bot = OnboardBot(hub_url="http://localhost:8000")
        await bot.ask("hi")
        assert bot._stats["questions_answered"] == 1
        assert bot._stats["ollama_success"] == 1

    @pytest.mark.asyncio
    async def test_ask_faq_fallback_updates_stats(self, requests_mock):
        requests_mock.post(
            "http://localhost:11434/api/chat",
            exc=requests.exceptions.ConnectionError("refused"),
        )
        bot = OnboardBot(hub_url="http://localhost:8000")
        await bot.ask("I'm getting 401 errors")
        assert bot._stats["questions_answered"] == 1
        assert bot._stats["faq_fallback"] == 1
        assert bot._stats["errors"] == 0


class TestOnboardBotHealth:
    @pytest.mark.asyncio
    async def test_health_ollama_up(self, requests_mock):
        requests_mock.get(
            "http://localhost:11434/api/tags",
            json={"models": [{"name": "llama3.2:1b"}, {"name": "phi:2.7b"}]},
            status_code=200,
        )
        bot = OnboardBot(hub_url="http://localhost:8000")
        health = await bot.health()
        assert health["ollama_reachable"] is True
        assert "llama3.2:1b" in health["available_models"]
        assert health["model"] == "llama3.2:1b"
        assert health["base_url"] == "http://localhost:11434"

    @pytest.mark.asyncio
    async def test_health_ollama_down(self, requests_mock):
        requests_mock.get(
            "http://localhost:11434/api/tags",
            exc=requests.exceptions.ConnectionError("refused"),
        )
        bot = OnboardBot(hub_url="http://localhost:8000")
        health = await bot.health()
        assert health["ollama_reachable"] is False
        assert health["available_models"] == []


class TestOnboardBotLifecycle:
    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        bot = OnboardBot(hub_url="http://localhost:8000")
        await bot.start()
        await bot.start()  # should not raise
        assert bot._started is True
        await bot.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        bot = OnboardBot(hub_url="http://localhost:8000")
        await bot.start()
        await bot.stop()
        assert bot._started is False
        assert bot._task.done()


class TestOnboardBotFAQ:
    def test_get_faq_returns_list(self):
        faq = OnboardBot.get_faq()
        assert isinstance(faq, list)
        assert len(faq) == len(FAQ_ANSWERS)

    def test_faq_items_have_required_fields(self):
        for item in OnboardBot.get_faq():
            assert "id" in item
            assert "question" in item
            assert "answer" in item
            assert item["id"] in [i["id"] for i in OnboardBot.get_faq()]  # unique ids


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------
class TestConfigDefaults:
    def test_default_node_id(self):
        from hub.onboard_bot import _NODE_ID
        assert _NODE_ID == "node_onboard_bot"

    def test_default_alias(self):
        from hub.onboard_bot import _ALIAS
        assert _ALIAS == "MEP Onboard Bot"

    def test_system_prompt_contains_alias(self):
        from hub.onboard_bot import SYSTEM_PROMPT
        assert "MEP Onboard Bot" in SYSTEM_PROMPT
        assert "MEP" in SYSTEM_PROMPT

    def test_faq_answers_all_populated(self):
        assert len(FAQ_ANSWERS) >= 8
        for k, v in FAQ_ANSWERS.items():
            assert isinstance(k, str)
            assert isinstance(v, str)
            assert len(v) > 10
