import asyncio
import json
import os
import time
import urllib.parse
import uuid
from typing import Any, Awaitable, Callable, Optional

import requests

from clients.shared.identity import MEPIdentity
from node.task_envelope import build_task_envelope
from node.ws_connect import ws_connect

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")
WS_URL = os.getenv("WS_URL", "wss://mep-hub.silentcopilot.ai")
WS_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("MEP_WS_HEARTBEAT_INTERVAL_SECONDS", "60"))
REVIEW_VERDICTS = {"approve", "approve_with_conditions", "request_changes", "block"}


class MEPClient:
    def __init__(self, key_path: str):
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.session = requests.Session()
        self.task_channels: dict[str, str] = {}
        self._stop = asyncio.Event()

    async def register(self) -> dict:
        response = await asyncio.to_thread(
            self.session.post,
            f"{HUB_URL}/register",
            json={"pubkey": self.identity.pub_pem},
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
        model_requirement: Optional[str] = None,
        target_node: Optional[str] = None,
        *,
        payload_uri: Optional[str] = None,
        secret_data: Optional[str] = None,
    ) -> dict:
        body = build_task_envelope(
            self.node_id,
            payload,
            bounty,
            target_node=target_node,
            target_capability=model_requirement,
            payload_uri=payload_uri,
            secret_data=secret_data,
        )
        payload_str = json.dumps(body)
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.post,
            f"{HUB_URL}/tasks/submit",
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
            f"{HUB_URL}/tasks/cancel",
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
            f"{HUB_URL}/tasks/result/{task_id}",
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    async def get_balance(self) -> dict:
        payload_str = ""
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.get,
            f"{HUB_URL}/balance/{self.node_id}",
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
            f"{HUB_URL}/brainstorm/sessions/create",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    def build_interbot_message(
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
        human_note: Optional[str] = None,
        trace_id: Optional[str] = None,
        task_title: Optional[str] = None,
        task_inputs: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        task: dict[str, Any] = {
            "instructions": message,
            "expected_output": {"result_type": result_type},
        }
        if task_title:
            task["title"] = task_title
        if task_inputs:
            task["inputs"] = task_inputs
        return {
            "spec_version": "mep.interbot.v1",
            "message_id": message_id,
            "trace_id": trace_id or str(uuid.uuid4()),
            "timestamp_ms": int(time.time() * 1000),
            "source": {"node_id": self.node_id},
            "target": {
                "node_id": target_node,
                **({"alias": target_alias} if target_alias else {}),
            },
            "conversation": {
                "context_id": context_id or message_id,
                "reply_to_task_id": reply_to_task_id,
                "reply_to_message_id": reply_to_message_id,
                "turn_type": turn_type,
            },
            "intent": {"type": intent_type, "priority": priority},
            "task": task,
            "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
            "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
            **({"human_note": human_note} if human_note else {}),
        }

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
        human_note: Optional[str] = None,
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
            human_note=human_note,
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
        source = inbound_message.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("node_id"), str):
            raise ValueError("inbound inter-bot message is missing source.node_id")
        inbound_intent = inbound_message.get("intent")
        inbound_priority = (
            inbound_intent.get("priority")
            if isinstance(inbound_intent, dict) and isinstance(inbound_intent.get("priority"), str)
            else "normal"
        )
        conversation = inbound_message.get("conversation")
        inbound_turn_type = conversation.get("turn_type") if isinstance(conversation, dict) else None
        return self.build_interbot_message(
            reply_text,
            source["node_id"],
            target_alias=source.get("alias") if isinstance(source.get("alias"), str) else None,
            intent_type=intent_type or self._default_reply_intent_type(
                inbound_intent.get("type") if isinstance(inbound_intent, dict) else None
            ),
            priority=priority or inbound_priority,
            context_id=conversation.get("context_id") if isinstance(conversation, dict) else None,
            reply_to_task_id=inbound_task_id,
            reply_to_message_id=inbound_message.get("message_id")
            if isinstance(inbound_message.get("message_id"), str)
            else None,
            turn_type=turn_type or self._default_reply_turn_type(inbound_turn_type),
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
        target = envelope["target"]["node_id"]
        response = await self.submit_task(json.dumps(envelope), 0.0, None, target)
        response["message_id"] = envelope["message_id"]
        response["trace_id"] = envelope["trace_id"]
        response["context_id"] = envelope["conversation"]["context_id"]
        return response

    def build_checkpoint_message(
        self,
        summary: str,
        target_node: str,
        *,
        context_id: str,
        target_alias: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        priority: str = "normal",
        human_note: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.build_interbot_message(
            summary,
            target_node,
            target_alias=target_alias,
            intent_type="coordination.request",
            priority=priority,
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type="checkpoint",
            human_note=human_note,
        )

    async def submit_checkpoint_dm(
        self,
        summary: str,
        target_node: str,
        *,
        context_id: str,
        target_alias: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        priority: str = "normal",
        human_note: Optional[str] = None,
    ) -> dict:
        envelope = self.build_checkpoint_message(
            summary,
            target_node,
            context_id=context_id,
            target_alias=target_alias,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            priority=priority,
            human_note=human_note,
        )
        response = await self.submit_task(json.dumps(envelope), 0.0, None, target_node)
        response["message_id"] = envelope["message_id"]
        response["trace_id"] = envelope["trace_id"]
        response["context_id"] = envelope["conversation"]["context_id"]
        return response

    def build_review_verdict_message(
        self,
        verdict: str,
        rationale: str,
        target_node: str,
        *,
        context_id: str,
        target_alias: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        conditions: Optional[list[str]] = None,
        human_recommendation: Optional[str] = None,
        priority: str = "normal",
        human_note: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_verdict = verdict.strip().lower()
        if normalized_verdict not in REVIEW_VERDICTS:
            raise ValueError(f"unsupported review verdict: {verdict}")

        normalized_rationale = rationale.strip()
        if not normalized_rationale:
            raise ValueError("review rationale must be non-empty")

        normalized_conditions = self._normalize_string_list(conditions)
        normalized_recommendation = (
            human_recommendation.strip() if isinstance(human_recommendation, str) else None
        )
        verdict_payload: dict[str, Any] = {
            "decision": normalized_verdict,
            "rationale": normalized_rationale,
            "conditions": normalized_conditions,
        }
        if normalized_recommendation:
            verdict_payload["human_recommendation"] = normalized_recommendation

        message_lines = [
            f"Review verdict: {normalized_verdict}",
            f"Rationale: {normalized_rationale}",
        ]
        if normalized_conditions:
            message_lines.append("Conditions:")
            message_lines.extend(f"- {condition}" for condition in normalized_conditions)
        if normalized_recommendation:
            message_lines.append(f"Human recommendation: {normalized_recommendation}")

        return self.build_interbot_message(
            "\n".join(message_lines),
            target_node,
            target_alias=target_alias,
            intent_type="review.response",
            priority=priority,
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type="approval",
            result_type="text",
            human_note=human_note,
            task_title="Review verdict",
            task_inputs={"review_verdict": verdict_payload},
        )

    async def submit_review_verdict_dm(
        self,
        verdict: str,
        rationale: str,
        target_node: str,
        *,
        context_id: str,
        target_alias: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        conditions: Optional[list[str]] = None,
        human_recommendation: Optional[str] = None,
        priority: str = "normal",
        human_note: Optional[str] = None,
    ) -> dict:
        envelope = self.build_review_verdict_message(
            verdict,
            rationale,
            target_node,
            context_id=context_id,
            target_alias=target_alias,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            conditions=conditions,
            human_recommendation=human_recommendation,
            priority=priority,
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
            f"{HUB_URL}/brainstorm/sessions/post",
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
            f"{HUB_URL}/brainstorm/sessions/{session_id}",
            params={"limit": limit},
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}

    @classmethod
    def parse_interbot_payload(cls, payload_text: str) -> Optional[dict[str, Any]]:
        try:
            parsed = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("spec_version") != "mep.interbot.v1":
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

    @classmethod
    def extract_review_verdict(cls, payload_text: str) -> Optional[dict[str, Any]]:
        parsed = cls.parse_interbot_payload(payload_text)
        if not parsed:
            return None
        task = parsed.get("task")
        if not isinstance(task, dict):
            return None
        inputs = task.get("inputs")
        if not isinstance(inputs, dict):
            return None
        review_verdict = inputs.get("review_verdict")
        if not isinstance(review_verdict, dict):
            return None
        decision = review_verdict.get("decision")
        rationale = review_verdict.get("rationale")
        if not isinstance(decision, str) or decision not in REVIEW_VERDICTS:
            return None
        if not isinstance(rationale, str) or not rationale.strip():
            return None
        extracted: dict[str, Any] = {
            "decision": decision,
            "rationale": rationale.strip(),
            "conditions": cls._normalize_string_list(review_verdict.get("conditions")),
        }
        human_recommendation = review_verdict.get("human_recommendation")
        if isinstance(human_recommendation, str) and human_recommendation.strip():
            extracted["human_recommendation"] = human_recommendation.strip()
        return extracted

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

    @staticmethod
    def _normalize_string_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        normalized: list[str] = []
        for value in values:
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    normalized.append(stripped)
        return normalized

    async def listen_results(
        self,
        on_result: Callable[[dict], Awaitable[None]],
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> None:
        while not self._stop.is_set():
            ts = str(int(time.time()))
            sig = urllib.parse.quote(self.identity.sign(self.node_id, ts))
            uri = f"{WS_URL}/ws/{self.node_id}?timestamp={ts}&signature={sig}"
            try:
                async with ws_connect(uri) as ws:
                    heartbeat_task: Optional[asyncio.Task] = None
                    if WS_HEARTBEAT_INTERVAL_SECONDS > 0:
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        while not self._stop.is_set():
                            msg = await ws.recv()
                            data = json.loads(msg)
                            if data.get("event") == "task_result":
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
