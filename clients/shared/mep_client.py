import asyncio
import json
import os
import time
import urllib.parse
import uuid
from typing import Any, Awaitable, Callable, Optional

import requests
import websockets

from clients.shared.dm_crypto import decode_dm_envelope, decrypt_dm_payload, encode_dm_envelope, encrypt_dm_payload
from clients.shared.identity import MEPIdentity

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")
WS_URL = os.getenv("WS_URL", "wss://mep-hub.silentcopilot.ai")
WS_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("MEP_WS_HEARTBEAT_INTERVAL_SECONDS", "60"))
PRIVACY_MODE_PLAINTEXT_ONLY = "plaintext_only"
PRIVACY_MODE_PREFER_ENCRYPTED = "prefer_encrypted"
PRIVACY_MODE_REQUIRE_ENCRYPTED = "require_encrypted"
VALID_PRIVACY_MODES = {
    PRIVACY_MODE_PLAINTEXT_ONLY,
    PRIVACY_MODE_PREFER_ENCRYPTED,
    PRIVACY_MODE_REQUIRE_ENCRYPTED,
}
INTERBOT_SPEC_VERSION = "mep.interbot.v1"
EXECUTION_RESULT_TYPE = "code_edit_status"
DEFAULT_EXECUTION_MUST_INCLUDE = [
    "workspace_opened",
    "file_edited",
    "branch",
    "commit_sha",
    "pr",
]


class MEPClient:
    def __init__(self, key_path: str, hub_url: Optional[str] = None, ws_url: Optional[str] = None):
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.session = requests.Session()
        self.task_channels: dict[str, str] = {}
        self._stop = asyncio.Event()
        self.hub_url = hub_url or HUB_URL
        self.ws_url = ws_url or WS_URL
        privacy_mode = os.getenv("MEP_PRIVACY_MODE", PRIVACY_MODE_PREFER_ENCRYPTED).strip().lower()
        self.privacy_mode = privacy_mode if privacy_mode in VALID_PRIVACY_MODES else PRIVACY_MODE_PREFER_ENCRYPTED

    async def register(self) -> dict:
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/register",
            json={"pubkey": self.identity.pub_pem, "x25519_public_key": self.identity.x25519_public_key},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _auth_headers(self, payload_str: str) -> dict:
        headers = self.identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        return headers

    async def submit_task(
        self,
        payload: str,
        bounty: float,
        model_requirement: Optional[str],
        target_node: Optional[str],
    ) -> dict:
        payload_to_send = payload
        if target_node and bounty == 0.0:
            payload_to_send = await self._prepare_dm_payload_for_target(payload, target_node)
        body: dict = {
            "consumer_id": self.node_id,
            "payload": payload_to_send,
            "bounty": bounty,
        }
        if model_requirement is not None:
            body["model_requirement"] = model_requirement
        if target_node is not None:
            body["target_node"] = target_node
        payload_str = json.dumps(body)
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/tasks/submit",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    async def cancel_task(self, task_id: str) -> dict:
        body = {"task_id": task_id}
        payload_str = json.dumps(body)
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/tasks/cancel",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    async def get_result(self, task_id: str) -> dict:
        payload_str = ""
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.get,
            f"{self.hub_url}/tasks/result/{task_id}",
            headers=headers,
            timeout=20,
        )
        data = response.json()
        if response.status_code == 200 and isinstance(data, dict) and isinstance(data.get("result_payload"), str):
            data["result_payload"] = self._maybe_decrypt_dm_payload(data["result_payload"])
        return {"status_code": response.status_code, "json": data}

    async def get_balance(self) -> dict:
        payload_str = ""
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.get,
            f"{self.hub_url}/balance/{self.node_id}",
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    async def create_brainstorm_session(
        self,
        participants: list[str],
        topic: Optional[str] = None,
        max_messages: int = 200,
    ) -> dict:
        body: dict = {
            "owner_id": self.node_id,
            "participants": participants,
            "max_messages": max_messages,
        }
        if topic:
            body["topic"] = topic
        payload_str = json.dumps(body)
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/brainstorm/sessions/create",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    def build_interbot_message(
        self,
        instructions: str,
        target_node: str,
        *,
        target_alias: Optional[str] = None,
        intent_type: str = "chat.request",
        priority: str = "normal",
        context_id: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        turn_type: str = "chat_turn",
        result_type: str = "text",
        title: Optional[str] = None,
        task_inputs: Optional[dict[str, Any]] = None,
        expected_output_must_include: Optional[list[str]] = None,
        constraints: Optional[dict[str, Any]] = None,
        human_note: Optional[str] = None,
        message_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        message_uuid = message_id or str(uuid.uuid4())
        conversation_context = context_id or str(uuid.uuid4())
        task_payload: dict[str, Any] = {
            "instructions": instructions,
            "expected_output": {
                "result_type": result_type,
            },
        }
        if title:
            task_payload["title"] = title
        if task_inputs:
            task_payload["inputs"] = dict(task_inputs)
        if expected_output_must_include:
            task_payload["expected_output"]["must_include"] = list(expected_output_must_include)
        if constraints:
            task_payload["constraints"] = dict(constraints)
        envelope: dict[str, Any] = {
            "spec_version": INTERBOT_SPEC_VERSION,
            "message_id": message_uuid,
            "trace_id": trace_id or message_uuid,
            "timestamp_ms": int(time.time() * 1000),
            "source": {
                "node_id": self.node_id,
                "alias": None,
            },
            "target": {
                "node_id": target_node,
                "alias": target_alias,
            },
            "conversation": {
                "context_id": conversation_context,
                "reply_to_task_id": reply_to_task_id,
                "reply_to_message_id": reply_to_message_id,
                "turn_type": turn_type,
            },
            "intent": {
                "type": intent_type,
                "priority": priority,
            },
            "task": task_payload,
            "economics": {
                "bounty_seconds": 0.0,
                "currency": "SECONDS",
            },
            "delivery": {
                "reply_mode": "new_dm",
                "settlement_mode": "task_result",
            },
        }
        if human_note:
            envelope["human_note"] = human_note
        return envelope

    async def submit_dm(
        self,
        message: str,
        target_node: str,
        *,
        target_alias: Optional[str] = None,
        intent_type: str = "chat.request",
        priority: str = "normal",
        context_id: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        turn_type: str = "chat_turn",
        result_type: str = "text",
        title: Optional[str] = None,
        task_inputs: Optional[dict[str, Any]] = None,
        expected_output_must_include: Optional[list[str]] = None,
        constraints: Optional[dict[str, Any]] = None,
        human_note: Optional[str] = None,
        message_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> dict:
        envelope = self.build_interbot_message(
            message,
            target_node,
            target_alias=target_alias,
            intent_type=intent_type,
            priority=priority,
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type=turn_type,
            result_type=result_type,
            title=title,
            task_inputs=task_inputs,
            expected_output_must_include=expected_output_must_include,
            constraints=constraints,
            human_note=human_note,
            message_id=message_id,
            trace_id=trace_id,
        )
        response = await self.submit_task(json.dumps(envelope), 0.0, None, target_node)
        response["message_id"] = envelope["message_id"]
        response["trace_id"] = envelope["trace_id"]
        response["context_id"] = envelope["conversation"]["context_id"]
        return response

    def build_interbot_reply_message(
        self,
        reply_text: str,
        inbound_message: dict[str, Any],
        *,
        inbound_task_id: Optional[str] = None,
        turn_type: Optional[str] = None,
        intent_type: Optional[str] = None,
        priority: Optional[str] = None,
        human_note: Optional[str] = None,
    ) -> dict[str, Any]:
        source = inbound_message.get("source") if isinstance(inbound_message, dict) else None
        if not isinstance(source, dict) or not isinstance(source.get("node_id"), str) or not source.get("node_id"):
            raise ValueError("inbound inter-bot message is missing source.node_id")
        inbound_intent = inbound_message.get("intent") if isinstance(inbound_message, dict) else None
        inbound_priority = (
            inbound_intent.get("priority")
            if isinstance(inbound_intent, dict) and isinstance(inbound_intent.get("priority"), str)
            else "normal"
        )
        conversation = inbound_message.get("conversation") if isinstance(inbound_message, dict) else None
        inbound_turn_type = conversation.get("turn_type") if isinstance(conversation, dict) else None
        reply_turn_type = turn_type or self._default_reply_turn_type(inbound_turn_type)
        reply_intent = intent_type or self._default_reply_intent_type(
            inbound_intent.get("type") if isinstance(inbound_intent, dict) else None
        )
        return self.build_interbot_message(
            reply_text,
            str(source["node_id"]),
            target_alias=source.get("alias") if isinstance(source.get("alias"), str) else None,
            intent_type=reply_intent,
            priority=priority or inbound_priority,
            context_id=conversation.get("context_id") if isinstance(conversation, dict) else None,
            reply_to_task_id=inbound_task_id,
            reply_to_message_id=inbound_message.get("message_id") if isinstance(inbound_message.get("message_id"), str) else None,
            turn_type=reply_turn_type,
            human_note=human_note,
            trace_id=inbound_message.get("trace_id") if isinstance(inbound_message.get("trace_id"), str) else None,
        )

    async def submit_dm_reply(
        self,
        reply_text: str,
        inbound_message: dict[str, Any],
        *,
        inbound_task_id: Optional[str] = None,
        turn_type: Optional[str] = None,
        intent_type: Optional[str] = None,
        priority: Optional[str] = None,
        human_note: Optional[str] = None,
    ) -> dict:
        envelope = self.build_interbot_reply_message(
            reply_text,
            inbound_message,
            inbound_task_id=inbound_task_id,
            turn_type=turn_type,
            intent_type=intent_type,
            priority=priority,
            human_note=human_note,
        )
        target = envelope.get("target") if isinstance(envelope.get("target"), dict) else {}
        target_node = target.get("node_id")
        if not isinstance(target_node, str) or not target_node:
            raise ValueError("reply envelope is missing target.node_id")
        response = await self.submit_task(json.dumps(envelope), 0.0, None, target_node)
        response["message_id"] = envelope["message_id"]
        response["trace_id"] = envelope["trace_id"]
        response["context_id"] = envelope["conversation"]["context_id"]
        return response

    def build_execution_request_message(
        self,
        instructions: str,
        target_node: str,
        *,
        target_alias: Optional[str] = None,
        context_id: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        turn_type: str = "operator_dm",
        title: Optional[str] = None,
        task_inputs: Optional[dict[str, Any]] = None,
        required_capabilities: Optional[list[str]] = None,
        must_include: Optional[list[str]] = None,
        max_runtime_seconds: Optional[int] = None,
        max_cost_seconds: Optional[float] = None,
        human_note: Optional[str] = None,
    ) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        capabilities = [
            item.strip()
            for item in (required_capabilities or ["code_edit"])
            if isinstance(item, str) and item.strip()
        ]
        if capabilities:
            constraints["required_capabilities"] = capabilities
        if max_runtime_seconds is not None:
            constraints["max_runtime_seconds"] = int(max_runtime_seconds)
        if max_cost_seconds is not None:
            constraints["max_cost_seconds"] = float(max_cost_seconds)
        return self.build_interbot_message(
            instructions,
            target_node,
            target_alias=target_alias,
            intent_type="coordination.request",
            priority="normal",
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type=turn_type,
            result_type=EXECUTION_RESULT_TYPE,
            title=title,
            task_inputs=task_inputs,
            expected_output_must_include=must_include or list(DEFAULT_EXECUTION_MUST_INCLUDE),
            constraints=constraints or None,
            human_note=human_note,
        )

    async def submit_execution_dm(
        self,
        instructions: str,
        target_node: str,
        *,
        target_alias: Optional[str] = None,
        context_id: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        turn_type: str = "operator_dm",
        title: Optional[str] = None,
        task_inputs: Optional[dict[str, Any]] = None,
        required_capabilities: Optional[list[str]] = None,
        must_include: Optional[list[str]] = None,
        max_runtime_seconds: Optional[int] = None,
        max_cost_seconds: Optional[float] = None,
        human_note: Optional[str] = None,
    ) -> dict:
        envelope = self.build_execution_request_message(
            instructions,
            target_node,
            target_alias=target_alias,
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type=turn_type,
            title=title,
            task_inputs=task_inputs,
            required_capabilities=required_capabilities,
            must_include=must_include,
            max_runtime_seconds=max_runtime_seconds,
            max_cost_seconds=max_cost_seconds,
            human_note=human_note,
        )
        response = await self.submit_task(json.dumps(envelope), 0.0, None, target_node)
        response["message_id"] = envelope["message_id"]
        response["trace_id"] = envelope["trace_id"]
        response["context_id"] = envelope["conversation"]["context_id"]
        return response

    async def post_brainstorm_message(
        self,
        session_id: str,
        message: str,
        reply_to_message_id: Optional[str] = None,
    ) -> dict:
        body: dict = {
            "session_id": session_id,
            "message": message,
        }
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        payload_str = json.dumps(body)
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/brainstorm/sessions/post",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    async def get_brainstorm_session(self, session_id: str, limit: int = 100) -> dict:
        payload_str = ""
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.get,
            f"{self.hub_url}/brainstorm/sessions/{session_id}",
            params={"limit": limit},
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    async def listen_results(
        self,
        on_result: Callable[[dict], Awaitable[None]],
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> None:
        while not self._stop.is_set():
            ts = str(int(time.time()))
            sig = urllib.parse.quote(self.identity.sign(self.node_id, ts))
            uri = f"{self.ws_url}/ws/{self.node_id}?timestamp={ts}&signature={sig}"
            try:
                async with websockets.connect(uri) as ws:
                    heartbeat_task: Optional[asyncio.Task] = None
                    if WS_HEARTBEAT_INTERVAL_SECONDS > 0:
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        while not self._stop.is_set():
                            msg = await ws.recv()
                            data = json.loads(msg)
                            if data.get("event") == "task_result":
                                task_data = data.get("data", {})
                                if isinstance(task_data, dict) and isinstance(task_data.get("result_payload"), str):
                                    task_data["result_payload"] = self._maybe_decrypt_dm_payload(task_data["result_payload"])
                                await on_result(data["data"])
                            elif on_event is not None:
                                await on_event(data)
                    finally:
                        if heartbeat_task:
                            heartbeat_task.cancel()
                            await asyncio.gather(heartbeat_task, return_exceptions=True)
            except Exception:
                await asyncio.sleep(2)

    async def _heartbeat_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(WS_HEARTBEAT_INTERVAL_SECONDS)
            await ws.send(json.dumps({"event": "heartbeat", "node_id": self.node_id, "ts": int(time.time())}))

    def stop(self) -> None:
        self._stop.set()

    def get_privacy_registry_metadata(self) -> dict:
        return {
            "privacy_mode": self.privacy_mode,
            "encryption_capabilities": {"dm": ["x25519-hkdf-sha256-aesgcm-v1"]},
            "x25519_public_key_present": bool(self.identity.x25519_public_key),
        }

    async def get_registry_entry(self, node_id: str) -> Optional[dict]:
        payload_str = ""
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.get,
            f"{self.hub_url}/registry/{node_id}",
            headers=headers,
            timeout=20,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    def _extract_registry_privacy_mode(self, entry: Optional[dict]) -> str:
        if not isinstance(entry, dict):
            return PRIVACY_MODE_PLAINTEXT_ONLY
        metadata = entry.get("metadata")
        if isinstance(metadata, dict):
            mode = metadata.get("privacy_mode")
            if isinstance(mode, str):
                normalized = mode.strip().lower()
                if normalized in VALID_PRIVACY_MODES:
                    return normalized
        return PRIVACY_MODE_PLAINTEXT_ONLY

    def _supports_encryption(self, entry: Optional[dict]) -> bool:
        if not isinstance(entry, dict):
            return False
        key = entry.get("x25519_public_key")
        return isinstance(key, str) and bool(key.strip())

    async def _prepare_dm_payload_for_target(self, plaintext: str, target_node: str) -> str:
        target_entry = await self.get_registry_entry(target_node)
        target_mode = self._extract_registry_privacy_mode(target_entry)
        target_supports_encryption = self._supports_encryption(target_entry)
        sender_prefers_encryption = self.privacy_mode in (
            PRIVACY_MODE_PREFER_ENCRYPTED,
            PRIVACY_MODE_REQUIRE_ENCRYPTED,
        )
        sender_requires_encryption = self.privacy_mode == PRIVACY_MODE_REQUIRE_ENCRYPTED
        target_requires_encryption = target_mode == PRIVACY_MODE_REQUIRE_ENCRYPTED

        if sender_prefers_encryption and target_supports_encryption:
            envelope = encrypt_dm_payload(plaintext, str(target_entry["x25519_public_key"]))
            return encode_dm_envelope(envelope)
        if sender_requires_encryption or target_requires_encryption:
            raise ValueError("Encrypted DM is required but peer negotiation failed")
        return plaintext

    async def prepare_dm_reply_payload(self, plaintext: str, peer_node_id: str, require_encrypted: bool = False) -> str:
        original_mode = self.privacy_mode
        try:
            if require_encrypted and self.privacy_mode == PRIVACY_MODE_PLAINTEXT_ONLY:
                self.privacy_mode = PRIVACY_MODE_REQUIRE_ENCRYPTED
            return await self._prepare_dm_payload_for_target(plaintext, peer_node_id)
        finally:
            self.privacy_mode = original_mode

    @staticmethod
    def parse_interbot_payload(payload_text: str) -> Optional[dict[str, Any]]:
        if not isinstance(payload_text, str):
            return None
        try:
            parsed = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("spec_version") != INTERBOT_SPEC_VERSION:
            return None
        return parsed

    @classmethod
    def extract_interbot_instructions(cls, payload_text: str) -> tuple[str, Optional[dict[str, Any]]]:
        parsed = cls.parse_interbot_payload(payload_text)
        if not parsed:
            return payload_text, None
        task = parsed.get("task")
        if isinstance(task, dict):
            instructions = task.get("instructions")
            if isinstance(instructions, str) and instructions.strip():
                return instructions.strip(), parsed
        return payload_text, parsed

    @staticmethod
    def _default_reply_intent_type(inbound_intent_type: Optional[str]) -> str:
        if inbound_intent_type == "review.request":
            return "review.response"
        return "chat.request"

    @staticmethod
    def _default_reply_turn_type(inbound_turn_type: Optional[str]) -> str:
        if inbound_turn_type == "review_request":
            return "review_response"
        return "chat_turn"

    def _maybe_decrypt_dm_payload(self, payload_text: str) -> str:
        envelope = decode_dm_envelope(payload_text)
        if not envelope:
            return payload_text
        try:
            return decrypt_dm_payload(envelope, self.identity.x25519_private_key)
        except Exception:
            return "[Encrypted DM received but decryption failed]"
