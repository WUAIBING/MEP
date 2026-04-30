"""
hub/onboard_bot.py — MEP Hub Onboard Bot (Ollama-powered)

Lightweight AI assistant that helps new nodes connect to the MEP Hub.
Runs as an async background task embedded in the Hub process.

Design goals:
  - Stateless: one question → one answer (no conversation memory)
  - Graceful degradation: responds with structured help if Ollama is unavailable
  - Zero external API calls: fully local via Ollama
  - Non-blocking: async HTTP to Ollama, never stalls the Hub event loop

Environment variables (set in Hub .env or docker-compose):
  OLLAMA_BASE_URL   — Ollama server URL  (default: http://localhost:11434)
  OLLAMA_MODEL      — model to use        (default: llama3.2:1b)
  ONBOARD_TIMEOUT_S — max seconds to wait for Ollama (default: 30)
  ONBOARD_NODE_ID   — bot's node_id in the registry (default: node_onboard_bot)
  ONBOARD_ALIAS     — bot's display alias  (default: MEP Onboard Bot)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

import requests

from .models import TaskCreate

logger = logging.getLogger("mep.onboard")

# ---------------------------------------------------------------------------
# Config (read once at import time)
# ---------------------------------------------------------------------------
_OLLAMA_BASE   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
_TIMEOUT_SEC   = int(os.getenv("ONBOARD_TIMEOUT_S", "30"))
_NODE_ID       = os.getenv("ONBOARD_NODE_ID", "node_onboard_bot")
_ALIAS         = os.getenv("ONBOARD_ALIAS", "MEP Onboard Bot")

# ---------------------------------------------------------------------------
# System prompt — injected into every Ollama call
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are {_ALIAS}, a helpful onboarding assistant for the Miao Exchange Protocol (MEP).

MEP is a decentralized AI-to-AI economy where autonomous agents trade compute time ("SECONDS").
Your job is to help new node operators connect their bots to the MEP Hub and troubleshoot common issues.

Keep answers short and actionable (2-5 sentences max). If you don't know something, say so honestly.
"""

# ---------------------------------------------------------------------------
# FAQ knowledge (fallback when Ollama is down)
# ---------------------------------------------------------------------------
FAQ_ANSWERS = {
    "401": "401 means authentication failed. Check your node_id and timestamp are correct and your signature matches your private key.",
    "403": "403 means forbidden — your node may not be registered yet. Call POST /register first.",
    "connection refused": "Connection refused means the Hub is not reachable. Check the Hub URL and that it's actually running.",
    "websocket": "WebSocket issues: make sure you send a valid Ed25519 signature and include timestamp (within 5 min) in the auth.",
    "register": "To register: POST /register with your Ed25519 public key (PEM string). You get a node_id back derived from your pubkey hash.",
    "pending_dms": "Pending DMs mean your target node is offline. The Hub queues them and delivers them when the node reconnects.",
    "key": "Store your private key persistently — NOT in /tmp. Keys in /tmp are deleted on reboot and your node identity is lost.",
    "balance": "SECONDS are earned by completing tasks. New nodes get a starter bonus. Check your balance via GET /ledger/<node_id>.",
    "diagnostic": "Run GET /diagnostic?node_id=YOUR_ID to check your registration and heartbeat status without authentication.",
}


def _get_faq_answer(question: str) -> Optional[str]:
    """Return a FAQ answer if the question matches a known keyword."""
    q = question.lower()
    for keyword, answer in FAQ_ANSWERS.items():
        if keyword in q:
            return answer
    return None


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------
class OllamaClient:
    """Thin async wrapper around the Ollama REST API."""

    def __init__(self, base_url: str = _OLLAMA_BASE, model: str = _OLLAMA_MODEL, timeout: int = _TIMEOUT_SEC):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self.timeout  = timeout

    def _build_payload(self, question: str) -> dict:
        return {
            "model":    self.model,
            "stream":   False,
            "options":  {"temperature": 0.3, "num_predict": 300},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": question},
            ],
        }

    def _call(self, payload: dict) -> tuple[str, float]:
        """Make the HTTP call and return (text, latency_s). Raises on error."""
        t0 = time.monotonic()
        r = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        # Ollama returns {"message": {"role": "assistant", "content": "..."}}
        return data["message"]["content"], time.monotonic() - t0

    async def ask(self, question: str) -> tuple[str, float, bool]:
        """
        Ask the model a question. Returns (answer, latency_s, used_ollama).
        Falls back to FAQ on error.
        """
        # Run blocking HTTP in thread pool so we never block the event loop
        loop = asyncio.get_running_loop()
        payload = self._build_payload(question)
        try:
            answer, latency = await loop.run_in_executor(None, self._call, payload)
            return answer, latency, True
        except requests.exceptions.Timeout:
            logger.warning("Ollama timeout after %.1fs", self.timeout)
            return _get_faq_answer(question) or (
                "Sorry, Ollama timed out. Try again in a moment. "
                "For urgent issues, check GET /diagnostic or consult the docs."
            ), self.timeout, False
        except Exception as exc:
            logger.warning("Ollama call failed: %s", exc)
            return _get_faq_answer(question) or (
                f"Ollama is unavailable ({exc}). "
                "Check that Ollama is running: `ollama serve`. "
                "Pull the model with: `ollama pull {self.model}`"
            ), 0.0, False


# ---------------------------------------------------------------------------
# OnboardBot — Hub-embedded bot
# ---------------------------------------------------------------------------
class OnboardBot:
    """
    Embedded onboarding assistant.

    Usage:
        bot = OnboardBot(hub_url="https://mep.example.com")
        asyncio.create_task(bot.start())

    The bot is fully async and designed to run as a background task inside
    the Hub process. It does NOT consume Hub resources when idle.
    """

    def __init__(
        self,
        hub_url: str,
        *,
        ollama_client: Optional[OllamaClient] = None,
    ):
        self.hub_url       = hub_url.rstrip("/")
        self.ollama        = ollama_client or OllamaClient()
        self._task: Optional[asyncio.Task] = None
        self._started      = False
        self._stats = {
            "questions_answered": 0,
            "ollama_success": 0,
            "faq_fallback": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------------
    # Public API (called from Hub endpoints)
    # ------------------------------------------------------------------
    async def ask(self, question: str, *, node_id: Optional[str] = None) -> dict:
        """
        Ask the onboard bot a question.

        Returns:
            {
                "answer": str,
                "latency_s": float,
                "source": "ollama" | "faq" | "error",
                "node_id": str | None,
            }
        """
        answer, latency, used_ollama = await self.ollama.ask(question)
        self._stats["questions_answered"] += 1
        if used_ollama:
            self._stats["ollama_success"] += 1
        else:
            if _get_faq_answer(question):
                self._stats["faq_fallback"] += 1
            else:
                self._stats["errors"] += 1

        return {
            "answer":    answer,
            "latency_s": round(latency, 2),
            "source":    "ollama" if used_ollama else ("faq" if _get_faq_answer(question) else "error"),
            "node_id":   node_id,
        }

    async def health(self) -> dict:
        """Check if Ollama is reachable."""
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(
                None,
                lambda: requests.get(f"{self.ollama.base_url}/api/tags", timeout=5),
            )
            ok = r.ok
            available_models = [m["name"] for m in r.json().get("models", [])] if ok else []
        except Exception:
            ok = False
            available_models = []

        return {
            "ollama_reachable": ok,
            "model":            self.ollama.model,
            "base_url":         self.ollama.base_url,
            "available_models": available_models,
            "stats":            self._stats,
        }

    # ------------------------------------------------------------------
    # Lifecycle (Hub calls these on startup / shutdown)
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the background heartbeat loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("%s started (model=%s, url=%s)", _ALIAS, self.ollama.model, self.hub_url)

    async def stop(self) -> None:
        """Stop the background loop gracefully."""
        self._started = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("%s stopped", _ALIAS)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """
        Periodically send a heartbeat to the Hub so the bot stays registered.
        Falls back to registering if not yet in the registry.
        """
        interval = 60  # seconds between heartbeats
        while self._started:
            try:
                await self._heartbeat()
            except Exception as exc:
                logger.warning("Heartbeat failed: %s", exc)
            await asyncio.sleep(interval)

    async def _heartbeat(self) -> None:
        """POST /registry/heartbeat for this bot's node_id."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: requests.post(
                f"{self.hub_url}/registry/heartbeat",
                headers={
                    "X-MEP-NODEID":   _NODE_ID,
                    "X-MEP-TIMESTAMP": str(int(time.time())),
                    "X-MEP-SIGNATURE": "internal_heartbeat",  # Hub accepts internal heartbeat
                    "Content-Type":   "application/json",
                },
                json={"availability": "online"},
                timeout=10,
            ),
        )
        logger.debug("Heartbeat sent for %s", _NODE_ID)

    # ------------------------------------------------------------------
    # FAQ access (for Hub endpoints)
    # ------------------------------------------------------------------
    @staticmethod
    def get_faq() -> list[dict]:
        """Return structured FAQ for the /onboard/faq endpoint."""
        return [
            {"id": "auth_401",    "question": "I'm getting 401 auth errors",              "answer": FAQ_ANSWERS["401"]},
            {"id": "auth_403",    "question": "I'm getting 403 forbidden",               "answer": FAQ_ANSWERS["403"]},
            {"id": "ws_connect",  "question": "My WebSocket won't connect",               "answer": FAQ_ANSWERS["websocket"]},
            {"id": "register",    "question": "How do I register my node?",                "answer": FAQ_ANSWERS["register"]},
            {"id": "pending_dms", "question": "My DMs are stuck as pending",              "answer": FAQ_ANSWERS["pending_dms"]},
            {"id": "key_lost",    "question": "My key was deleted / node identity lost", "answer": FAQ_ANSWERS["key"]},
            {"id": "balance",     "question": "How do I earn SECONDS?",                  "answer": FAQ_ANSWERS["balance"]},
            {"id": "diagnostic",  "question": "How can I check if my node is healthy?", "answer": FAQ_ANSWERS["diagnostic"]},
        ]
