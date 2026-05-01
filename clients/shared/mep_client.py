import asyncio
import json
import os
import time
import urllib.parse
from typing import Awaitable, Callable, Optional

import requests
import websockets

from clients.shared.identity import MEPIdentity
from clients.shared.manifest import load_manifest
WS_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("MEP_WS_HEARTBEAT_INTERVAL_SECONDS", "60"))


class MEPClient:
    def __init__(self, key_path: str, hub_url: Optional[str] = None, ws_url: Optional[str] = None):
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.session = requests.Session()
        self.session.trust_env = False
        self.task_channels: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        manifest = load_manifest()
        self.hub_url = (
            hub_url
            or os.getenv("HUB_URL")
            or (manifest.hub_url if manifest else None)
            or "https://mep-hub.silentcopilot.ai"
        )
        self.ws_url = (
            ws_url
            or os.getenv("WS_URL")
            or (manifest.ws_url if manifest else None)
            or "wss://mep-hub.silentcopilot.ai"
        )
        self.heartbeat_seconds = int(
            os.getenv("MEP_HEARTBEAT_SECONDS")
            or (manifest.heartbeat_seconds if manifest else 30)
            or 30
        )

    async def register(self) -> dict:
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/register",
            json={"pubkey": self.identity.pub_pem},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _auth_headers(self, payload_str: str) -> dict:
        headers = self.identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        return headers

    async def heartbeat_loop(self, availability: str = "online") -> None:
        while not self._stop.is_set():
            body = {"availability": availability}
            payload_str = json.dumps(body)
            try:
                response = await asyncio.to_thread(
                    self.session.post,
                    f"{self.hub_url}/registry/heartbeat",
                    data=payload_str,
                    headers=self._auth_headers(payload_str),
                    timeout=15,
                )
                response.raise_for_status()
            except Exception:
                # Best-effort keepalive; receiver/reconnect loop handles hard failures.
                pass
            await asyncio.sleep(max(1, self.heartbeat_seconds))

    def start_heartbeat(self, availability: str = "online") -> asyncio.Task:
        if self._heartbeat_task and not self._heartbeat_task.done():
            return self._heartbeat_task
        self._heartbeat_task = asyncio.create_task(self.heartbeat_loop(availability=availability))
        return self._heartbeat_task

    async def submit_task(
        self,
        payload: str,
        bounty: float,
        model_requirement: Optional[str],
        target_node: Optional[str],
    ) -> dict:
        body: dict = {
            "consumer_id": self.node_id,
            "payload": payload,
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
        return {"status_code": response.status_code, "json": response.json()}

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

    async def listen_results(self, on_result: Callable[[dict], Awaitable[None]]) -> None:
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
                                await on_result(data["data"])
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
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
