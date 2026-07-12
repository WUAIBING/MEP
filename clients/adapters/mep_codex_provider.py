import asyncio
import json
import os
import re
import tempfile
import time
import urllib.parse
from typing import Any

from clients.shared.dm_crypto import decode_dm_envelope
from clients.shared.execution_bridge import (
    build_execution_bridge_request,
    execute_bridge_command,
    is_execution_request,
    render_execution_result,
)
from clients.shared.mep_client import MEPClient
from clients.shared.manifest import load_manifest

os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ["NO_PROXY"] = "*"


class CodexProvider:
    def __init__(self) -> None:
        self.manifest = load_manifest()
        key_path = (
            os.getenv("MEP_BOT_KEY_PATH")
            or (self.manifest.key_path if self.manifest else None)
            or os.path.join(tempfile.gettempdir(), "mep_codex_provider.pem")
        )
        hub_url = os.getenv("HUB_URL") or (self.manifest.hub_url if self.manifest else None)
        ws_url = os.getenv("WS_URL") or (self.manifest.ws_url if self.manifest else None)
        self.client = MEPClient(key_path, hub_url=hub_url, ws_url=ws_url)
        alias_prefix = (
            os.getenv("MEP_ALIAS_PREFIX")
            or (self.manifest.alias if self.manifest else None)
            or "Master Wu Codex Bot"
        )
        if "{node_id}" in alias_prefix:
            self.alias = alias_prefix.replace("{node_id}", self.client.node_id)
        elif self.manifest and self.manifest.alias:
            self.alias = alias_prefix
        else:
            self.alias = f"{alias_prefix} {self.client.node_id}"
        self.heartbeat_seconds = int(
            os.getenv("MEP_HEARTBEAT_SECONDS")
            or (self.manifest.heartbeat_seconds if self.manifest else 30)
            or 30
        )
        runtime = self.manifest.runtime if self.manifest else {}
        self.runtime_config = runtime
        self.openai_base_url = (
            os.getenv("OPENAI_BASE_URL")
            or runtime.get("openai_base_url")
            or "https://api.openai.com/v1"
        )
        self.openai_model = (
            os.getenv("OPENAI_MODEL")
            or runtime.get("openai_model")
            or "gpt-4.1-mini"
        )
        self.openai_api_mode = (
            os.getenv("OPENAI_API_MODE")
            or runtime.get("openai_api_mode")
            or "responses"
        ).strip().lower()
        self.registry_skills = (
            self.manifest.registry_skills if self.manifest and self.manifest.registry_skills else ["dm", "chat", "codex"]
        )
        self.registry_models = (
            self.manifest.registry_models if self.manifest and self.manifest.registry_models else ["codex", "codex-agent"]
        )
        self.metadata = {"operator": "codex", "mode": "dm-auto-reply"}
        self.metadata.update(self.client.get_privacy_registry_metadata())
        if self.manifest:
            self.metadata.update(self.manifest.metadata)
        self._stop = asyncio.Event()
        self._llm_api_key = os.getenv("OPENAI_API_KEY")
        self.reasoning_effort = os.getenv("MEP_REASONING_EFFORT", "high").strip().lower()
        self.enable_deepseek_thinking = (
            os.getenv("MEP_DEEPSEEK_THINKING", "true").strip().lower() in ("1", "true", "yes")
        )

    def _log(self, message: str) -> None:
        try:
            print(message)
        except UnicodeEncodeError:
            # Keep provider alive on Windows consoles that cannot emit some Unicode.
            fallback = message.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
            print(fallback)

    def _auth_headers(self, payload_str: str) -> dict:
        return self.client._auth_headers(payload_str)

    def _clean_model_text(self, text: str) -> str:
        cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    async def _generate_reply(self, payload: str, consumer_id: str, task_id: str) -> str:
        text = payload.strip()
        if not text:
            return f"{self.alias} online. Empty DM received from {consumer_id} for task {task_id}."
        if not self._llm_api_key:
            return (
                f"{self.alias} online, but no OPENAI_API_KEY is configured on this node yet. "
                f"I received your message from {consumer_id}: {text}"
            )

        system_prompt = (
            "You are a real MEP DM chat node. Reply naturally, directly, and helpfully. "
            "Answer the user's task instead of acknowledging routing mechanics unless needed."
        )
        headers = {
            "Authorization": f"Bearer {self._llm_api_key}",
            "Content-Type": "application/json",
        }
        try:
            endpoint = f"{self.openai_base_url.rstrip('/')}/responses"
            body: dict[str, Any]
            if self.openai_api_mode == "chat_completions":
                endpoint = f"{self.openai_base_url.rstrip('/')}/chat/completions"
                body = {
                    "model": self.openai_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                    "stream": False,
                }
                base_url_normalized = self.openai_base_url.rstrip("/").lower()
                if self.enable_deepseek_thinking and "api.deepseek.com" in base_url_normalized:
                    body["thinking"] = {"type": "enabled"}
                    body["reasoning_effort"] = self.reasoning_effort
            else:
                body = {
                    "model": self.openai_model,
                    "input": [
                        {
                            "role": "system",
                            "content": [{"type": "input_text", "text": system_prompt}],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    ],
                }
            response = await asyncio.to_thread(
                self.client.session.post,
                endpoint,
                json=body,
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            if self.openai_api_mode == "chat_completions":
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return self._clean_model_text(content)
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, dict) and isinstance(item.get("text"), str):
                                parts.append(item["text"])
                        joined = "".join(parts).strip()
                        if joined:
                            return self._clean_model_text(joined)
            else:
                output_text = data.get("output_text")
                if isinstance(output_text, str) and output_text.strip():
                    return self._clean_model_text(output_text)
        except Exception as exc:
            return (
                f"{self.alias} received your task, but live model inference failed on this node: {exc}. "
                f"Original message: {text}"
            )
        return (
            f"{self.alias} received your task, but the model returned no text output. "
            f"Original message: {text}"
        )

    async def _handle_execution_request(
        self,
        interbot_message: dict[str, Any] | None,
        prompt: str,
        consumer_id: str,
        task_id: str,
    ) -> str | None:
        if not is_execution_request(interbot_message):
            return None
        request_payload = build_execution_bridge_request(
            interbot_message or {},
            consumer_id=consumer_id,
            task_id=task_id,
            prompt=prompt,
        )
        result = await execute_bridge_command(
            request_payload,
            runtime_config=self.runtime_config,
        )
        return render_execution_result(result)

    async def register(self) -> dict:
        response = await asyncio.to_thread(
            self.client.session.post,
            f"{self.client.hub_url}/register",
            json={
                "pubkey": self.client.identity.pub_pem,
                "alias": self.alias,
                "x25519_public_key": self.client.identity.x25519_public_key,
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    async def announce(self) -> dict:
        body = {
            "alias": self.alias,
            "skills": self.registry_skills,
            "models": self.registry_models,
            "metadata": self.metadata,
            "availability": "online",
        }
        payload_str = json.dumps(body)
        response = await asyncio.to_thread(
            self.client.session.post,
            f"{self.client.hub_url}/registry/update",
            data=payload_str,
            headers=self._auth_headers(payload_str),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    async def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            body = {"availability": "online"}
            payload_str = json.dumps(body)
            try:
                response = await asyncio.to_thread(
                    self.client.session.post,
                    f"{self.client.hub_url}/registry/heartbeat",
                    data=payload_str,
                    headers=self._auth_headers(payload_str),
                    timeout=15,
                )
                response.raise_for_status()
            except Exception as exc:
                self._log(f"[codex-provider] heartbeat failed: {exc}")
            await asyncio.sleep(self.heartbeat_seconds)

    async def complete_task(self, task: dict) -> None:
        task_id = task.get("id")
        consumer_id = task.get("consumer_id", "unknown")
        payload_raw = (task.get("payload") or "").strip()
        encrypted_inbound = bool(decode_dm_envelope(payload_raw))
        payload = self.client._maybe_decrypt_dm_payload(payload_raw).strip()
        prompt, interbot_message = self.client.extract_interbot_instructions(payload)
        execution_reply = await self._handle_execution_request(
            interbot_message,
            prompt,
            consumer_id,
            task_id,
        )
        reply = execution_reply if execution_reply is not None else await self._generate_reply(prompt, consumer_id, task_id)
        bounty = float(task.get("bounty", 1.0) or 0.0)
        is_dm = bounty == 0.0 and bool(task.get("target_node"))
        result_payload = reply
        if is_dm:
            result_payload = await self.client.prepare_dm_reply_payload(
                reply,
                consumer_id,
                require_encrypted=encrypted_inbound,
            )
        body = {
            "task_id": task_id,
            "provider_id": self.client.node_id,
            "result_payload": result_payload,
        }
        payload_str = json.dumps(body)
        response = await asyncio.to_thread(
            self.client.session.post,
            f"{self.client.hub_url}/tasks/complete",
            data=payload_str,
            headers=self._auth_headers(payload_str),
            timeout=20,
        )
        response.raise_for_status()
        self._log(f"[codex-provider] completed task {task_id} for {consumer_id}")

    async def ws_loop(self) -> None:
        import websockets

        while not self._stop.is_set():
            ts = str(int(time.time()))
            sig = urllib.parse.quote(self.client.identity.sign(self.client.node_id, ts))
            uri = f"{self.client.ws_url}/ws/{self.client.node_id}?timestamp={ts}&signature={sig}"
            try:
                async with websockets.connect(uri) as ws:
                    self._log(f"[codex-provider] websocket connected: {self.client.node_id}")
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        except asyncio.TimeoutError:
                            await ws.send("ping")
                            continue
                        data = json.loads(raw)
                        event = data.get("event")
                        task = data.get("data", {})
                        if event == "new_task":
                            await self.complete_task(task)
                        elif event == "task_result":
                            self._log(f"[codex-provider] task_result: {task}")
                        elif event == "rfc":
                            self._log(f"[codex-provider] rfc ignored: {task.get('id')}")
            except Exception as exc:
                self._log(f"[codex-provider] websocket disconnected: {exc}")
                await asyncio.sleep(3)

    async def run(self) -> None:
        registration = await self.register()
        self._log(
            f"[codex-provider] registered {registration.get('node_id')} alias='{self.alias}' "
            f"balance={registration.get('balance')}"
        )
        await self.announce()
        heartbeat = asyncio.create_task(self.heartbeat_loop())
        try:
            await self.ws_loop()
        finally:
            self._stop.set()
            heartbeat.cancel()


def main() -> None:
    asyncio.run(CodexProvider().run())


if __name__ == "__main__":
    main()
