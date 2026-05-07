import asyncio
import json
import os
import time
import urllib.parse
from typing import Awaitable, Callable, Optional

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

    def _maybe_decrypt_dm_payload(self, payload_text: str) -> str:
        envelope = decode_dm_envelope(payload_text)
        if not envelope:
            return payload_text
        try:
            return decrypt_dm_payload(envelope, self.identity.x25519_private_key)
        except Exception:
            return "[Encrypted DM received but decryption failed]"
