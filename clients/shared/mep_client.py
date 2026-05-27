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
HUMAN_APPROVAL_DECISION_TYPES = {"merge_decision", "deploy_decision", "policy_decision"}


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
        session_safety: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        if turn_index is not None and turn_index < 1:
            raise ValueError("turn_index must be at least 1")
        task: dict[str, Any] = {
            "instructions": message,
            "expected_output": {"result_type": result_type},
        }
        if task_title:
            task["title"] = task_title
        inputs: dict[str, Any] = dict(task_inputs or {})
        normalized_session_safety = self.build_session_safety_metadata(**session_safety) if session_safety else {}
        if normalized_session_safety and "started_at_ms" not in normalized_session_safety:
            normalized_session_safety["started_at_ms"] = timestamp_ms
        if normalized_session_safety:
            inputs["session_safety"] = normalized_session_safety
        if inputs:
            task["inputs"] = inputs
        return {
            "spec_version": "mep.interbot.v1",
            "message_id": message_id,
            "trace_id": trace_id or str(uuid.uuid4()),
            "timestamp_ms": timestamp_ms,
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
                **({"turn_index": turn_index} if turn_index is not None else {}),
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
        session_safety: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
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
            session_safety=session_safety,
            turn_index=turn_index,
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
        next_turn_index = self._derive_reply_turn_index(inbound_message)
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
            session_safety=self._extract_session_safety_from_message(inbound_message),
            turn_index=next_turn_index,
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

    async def submit_safe_dm_reply(
        self,
        reply_text: str,
        inbound_message: dict[str, Any],
        *,
        next_turn_index: int,
        checkpoint_summary: Optional[str] = None,
        inbound_task_id: Optional[str] = None,
        turn_type: Optional[str] = None,
        intent_type: Optional[str] = None,
        priority: Optional[str] = None,
        human_note: Optional[str] = None,
        now_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        evaluation = self.evaluate_interbot_session_safety_message(
            inbound_message,
            next_turn_index=next_turn_index,
            now_ms=now_ms,
        )
        context_id = self._extract_context_id(inbound_message)

        if evaluation["should_stop"]:
            return {
                "status": "stopped",
                "reply_action": "stop",
                "context_id": context_id,
                "session_safety": evaluation["session_safety"],
                "safety": evaluation,
            }

        if evaluation["should_checkpoint"]:
            source = inbound_message.get("source")
            if not isinstance(source, dict) or not isinstance(source.get("node_id"), str):
                raise ValueError("inbound inter-bot message is missing source.node_id")
            summary = (
                checkpoint_summary.strip()
                if isinstance(checkpoint_summary, str) and checkpoint_summary.strip()
                else f"Checkpoint: session reached turn {next_turn_index}. Confirm whether to continue."
            )
            checkpoint_response = await self.submit_checkpoint_dm(
                summary,
                source["node_id"],
                context_id=context_id,
                target_alias=source.get("alias") if isinstance(source.get("alias"), str) else None,
                reply_to_task_id=inbound_task_id,
                reply_to_message_id=inbound_message.get("message_id")
                if isinstance(inbound_message.get("message_id"), str)
                else None,
                priority=priority or "normal",
                human_note=human_note,
                session_safety=self._extract_session_safety_from_message(inbound_message),
                turn_index=next_turn_index,
            )
            checkpoint_response["status"] = "checkpointed"
            checkpoint_response["reply_action"] = "checkpoint"
            checkpoint_response["safety"] = evaluation
            return checkpoint_response

        response = await self.submit_dm_reply(
            reply_text,
            inbound_message,
            inbound_task_id=inbound_task_id,
            turn_type=turn_type,
            intent_type=intent_type,
            priority=priority,
            human_note=human_note,
        )
        response["status"] = "replied"
        response["reply_action"] = "reply"
        response["safety"] = evaluation
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
        session_safety: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
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
            session_safety=session_safety,
            turn_index=turn_index,
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
        session_safety: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
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
            session_safety=session_safety,
            turn_index=turn_index,
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
        turn_index: Optional[int] = None,
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
            turn_index=turn_index,
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
        turn_index: Optional[int] = None,
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
            turn_index=turn_index,
        )
        response = await self.submit_task(json.dumps(envelope), 0.0, None, target_node)
        response["message_id"] = envelope["message_id"]
        response["trace_id"] = envelope["trace_id"]
        response["context_id"] = envelope["conversation"]["context_id"]
        return response

    def build_human_approval_request_message(
        self,
        summary: str,
        target_node: str,
        *,
        context_id: str,
        decision_type: str = "merge_decision",
        target_alias: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        review_decision: Optional[str] = None,
        blockers: Optional[list[str]] = None,
        recommended_next_action: Optional[str] = None,
        priority: str = "high",
        human_note: Optional[str] = None,
        turn_index: Optional[int] = None,
    ) -> dict[str, Any]:
        normalized_summary = summary.strip()
        if not normalized_summary:
            raise ValueError("human approval summary must be non-empty")

        normalized_decision_type = decision_type.strip().lower()
        if normalized_decision_type not in HUMAN_APPROVAL_DECISION_TYPES:
            raise ValueError(f"unsupported human approval decision type: {decision_type}")

        normalized_review_decision = review_decision.strip().lower() if isinstance(review_decision, str) else None
        if normalized_review_decision and normalized_review_decision not in REVIEW_VERDICTS:
            raise ValueError(f"unsupported review decision: {review_decision}")

        normalized_blockers = self._normalize_string_list(blockers)
        normalized_next_action = (
            recommended_next_action.strip() if isinstance(recommended_next_action, str) else None
        )
        approval_payload: dict[str, Any] = {
            "decision_type": normalized_decision_type,
            "summary": normalized_summary,
            "blockers": normalized_blockers,
        }
        if normalized_review_decision:
            approval_payload["review_decision"] = normalized_review_decision
        if normalized_next_action:
            approval_payload["recommended_next_action"] = normalized_next_action

        message_lines = [
            f"Human approval request: {normalized_decision_type}",
            f"Summary: {normalized_summary}",
        ]
        if normalized_review_decision:
            message_lines.append(f"Proposed review decision: {normalized_review_decision}")
        if normalized_blockers:
            message_lines.append("Blockers:")
            message_lines.extend(f"- {blocker}" for blocker in normalized_blockers)
        if normalized_next_action:
            message_lines.append(f"Recommended next action: {normalized_next_action}")

        return self.build_interbot_message(
            "\n".join(message_lines),
            target_node,
            target_alias=target_alias,
            intent_type="human.approval.request",
            priority=priority,
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type="session_close",
            result_type="text",
            human_note=human_note,
            task_title="Human approval request",
            task_inputs={"human_approval_request": approval_payload},
            turn_index=turn_index,
        )

    async def submit_human_approval_request_dm(
        self,
        summary: str,
        target_node: str,
        *,
        context_id: str,
        decision_type: str = "merge_decision",
        target_alias: Optional[str] = None,
        reply_to_task_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        review_decision: Optional[str] = None,
        blockers: Optional[list[str]] = None,
        recommended_next_action: Optional[str] = None,
        priority: str = "high",
        human_note: Optional[str] = None,
        turn_index: Optional[int] = None,
    ) -> dict:
        envelope = self.build_human_approval_request_message(
            summary,
            target_node,
            context_id=context_id,
            decision_type=decision_type,
            target_alias=target_alias,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            review_decision=review_decision,
            blockers=blockers,
            recommended_next_action=recommended_next_action,
            priority=priority,
            human_note=human_note,
            turn_index=turn_index,
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

    @classmethod
    def extract_session_safety(cls, payload_text: str) -> Optional[dict[str, int]]:
        parsed = cls.parse_interbot_payload(payload_text)
        if not parsed:
            return None
        return cls._extract_session_safety_from_message(parsed)

    @classmethod
    def evaluate_interbot_session_safety_message(
        cls,
        message: dict[str, Any],
        *,
        next_turn_index: int,
        now_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        if next_turn_index < 1:
            raise ValueError("next_turn_index must be at least 1")

        session_safety = cls._extract_session_safety_from_message(message)
        if not session_safety:
            return {
                "session_safety": None,
                "next_turn_index": next_turn_index,
                "elapsed_ms": None,
                "should_checkpoint": False,
                "should_stop": False,
                "violations": [],
            }

        evaluated_now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        started_at_ms = session_safety.get("started_at_ms")
        if not isinstance(started_at_ms, int):
            started_at_ms = message.get("timestamp_ms") if isinstance(message.get("timestamp_ms"), int) else None
        elapsed_ms = (
            max(0, evaluated_now_ms - started_at_ms)
            if started_at_ms is not None and evaluated_now_ms >= started_at_ms
            else None
        )
        violations: list[str] = []

        max_turns = session_safety.get("max_turns")
        if isinstance(max_turns, int) and next_turn_index > max_turns:
            violations.append("max_turns_exceeded")

        max_duration_seconds = session_safety.get("max_duration_seconds")
        if isinstance(max_duration_seconds, int) and elapsed_ms is not None:
            if elapsed_ms > max_duration_seconds * 1000:
                violations.append("max_duration_exceeded")

        checkpoint_interval = session_safety.get("checkpoint_interval")
        should_checkpoint = (
            isinstance(checkpoint_interval, int)
            and checkpoint_interval > 0
            and next_turn_index > 1
            and next_turn_index % checkpoint_interval == 0
            and not violations
        )

        return {
            "session_safety": session_safety,
            "next_turn_index": next_turn_index,
            "elapsed_ms": elapsed_ms,
            "should_checkpoint": should_checkpoint,
            "should_stop": bool(violations),
            "violations": violations,
        }

    @classmethod
    def evaluate_interbot_session_safety(
        cls,
        payload_text: str,
        *,
        next_turn_index: int,
        now_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        if next_turn_index < 1:
            raise ValueError("next_turn_index must be at least 1")

        parsed = cls.parse_interbot_payload(payload_text)
        if not parsed:
            raise ValueError("payload_text is not a valid mep.interbot.v1 payload")
        return cls.evaluate_interbot_session_safety_message(
            parsed,
            next_turn_index=next_turn_index,
            now_ms=now_ms,
        )

    @classmethod
    def build_session_safety_metadata(
        cls,
        *,
        max_turns: Optional[int] = None,
        max_duration_seconds: Optional[int] = None,
        checkpoint_interval: Optional[int] = None,
        started_at_ms: Optional[int] = None,
    ) -> dict[str, int]:
        normalized: dict[str, int] = {}
        if max_turns is not None:
            normalized["max_turns"] = cls._normalize_positive_int(max_turns, "max_turns")
        if max_duration_seconds is not None:
            normalized["max_duration_seconds"] = cls._normalize_positive_int(
                max_duration_seconds, "max_duration_seconds"
            )
        if checkpoint_interval is not None:
            normalized["checkpoint_interval"] = cls._normalize_positive_int(
                checkpoint_interval, "checkpoint_interval"
            )
        if started_at_ms is not None:
            normalized["started_at_ms"] = cls._normalize_positive_int(started_at_ms, "started_at_ms")
        if not normalized:
            raise ValueError("at least one session safety guard must be provided")
        return normalized

    @classmethod
    def extract_human_approval_request(cls, payload_text: str) -> Optional[dict[str, Any]]:
        parsed = cls.parse_interbot_payload(payload_text)
        if not parsed:
            return None
        task = parsed.get("task")
        if not isinstance(task, dict):
            return None
        inputs = task.get("inputs")
        if not isinstance(inputs, dict):
            return None
        approval_request = inputs.get("human_approval_request")
        if not isinstance(approval_request, dict):
            return None
        decision_type = approval_request.get("decision_type")
        summary = approval_request.get("summary")
        if not isinstance(decision_type, str) or decision_type not in HUMAN_APPROVAL_DECISION_TYPES:
            return None
        if not isinstance(summary, str) or not summary.strip():
            return None
        extracted: dict[str, Any] = {
            "decision_type": decision_type,
            "summary": summary.strip(),
            "blockers": cls._normalize_string_list(approval_request.get("blockers")),
        }
        review_decision = approval_request.get("review_decision")
        if isinstance(review_decision, str) and review_decision in REVIEW_VERDICTS:
            extracted["review_decision"] = review_decision
        recommended_next_action = approval_request.get("recommended_next_action")
        if isinstance(recommended_next_action, str) and recommended_next_action.strip():
            extracted["recommended_next_action"] = recommended_next_action.strip()
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
    def _derive_reply_turn_index(inbound_message: dict[str, Any]) -> Optional[int]:
        conversation = inbound_message.get("conversation")
        if not isinstance(conversation, dict):
            return None
        turn_index = conversation.get("turn_index")
        if turn_index is None:
            return None
        if not isinstance(turn_index, int) or turn_index < 1:
            raise ValueError("inbound inter-bot message has invalid conversation.turn_index")
        return turn_index + 1

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

    @classmethod
    def _extract_session_safety_from_message(cls, message: dict[str, Any]) -> Optional[dict[str, int]]:
        task = message.get("task")
        if not isinstance(task, dict):
            return None
        inputs = task.get("inputs")
        if not isinstance(inputs, dict):
            return None
        session_safety = inputs.get("session_safety")
        if not isinstance(session_safety, dict):
            return None

        normalized: dict[str, int] = {}
        for field in ("max_turns", "max_duration_seconds", "checkpoint_interval", "started_at_ms"):
            value = session_safety.get(field)
            if value is None:
                continue
            try:
                normalized[field] = cls._normalize_positive_int(value, field)
            except ValueError:
                return None
        return normalized or None

    @staticmethod
    def _normalize_positive_int(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{field_name} must be a positive integer")
        return value

    @staticmethod
    def _extract_context_id(message: dict[str, Any]) -> Optional[str]:
        conversation = message.get("conversation")
        if isinstance(conversation, dict) and isinstance(conversation.get("context_id"), str):
            return conversation.get("context_id")
        return None

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
