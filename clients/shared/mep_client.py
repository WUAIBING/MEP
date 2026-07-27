import asyncio
import json
import os
import time
import urllib.parse
import uuid
from typing import Any, Awaitable, Callable, Optional

import requests

from clients.shared.dm_crypto import decode_dm_envelope, decrypt_dm_payload, encode_dm_envelope, encrypt_dm_payload
from clients.shared.identity import MEPIdentity
from clients.shared.manifest import load_manifest
from node.task_envelope import build_task_envelope
from node.ws_connect import ws_connect

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
REVIEW_VERDICTS = {"approve", "approve_with_conditions", "request_changes", "block"}
HUMAN_APPROVAL_DECISION_TYPES = {"merge_decision", "deploy_decision", "policy_decision"}
GOVERNANCE_CLASSIFICATIONS = {"safe", "approval_required", "forbidden"}
GOVERNANCE_APPROVAL_STATUSES = {"pending", "approved", "denied"}


class MEPClient:
    def __init__(self, key_path: str, hub_url: Optional[str] = None, ws_url: Optional[str] = None):
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.session = requests.Session()
        self.session.trust_env = False
        self.task_channels: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._active_ws = None
        self._heartbeat_task: asyncio.Task | None = None
        self._live_call_contexts: set[str] = set()
        manifest = load_manifest()
        self._manifest = manifest
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
        privacy_from_manifest = None
        if manifest and isinstance(manifest.raw.get("privacy"), dict):
            mode_raw = manifest.raw["privacy"].get("mode")
            if isinstance(mode_raw, str) and mode_raw.strip():
                privacy_from_manifest = mode_raw.strip().lower()
        self.privacy_mode = (
            os.getenv("MEP_PRIVACY_MODE", "").strip().lower()
            or privacy_from_manifest
            or PRIVACY_MODE_PREFER_ENCRYPTED
        )
        if self.privacy_mode not in VALID_PRIVACY_MODES:
            self.privacy_mode = PRIVACY_MODE_PREFER_ENCRYPTED

    async def register(self) -> dict:
        body = {
            "pubkey": self.identity.pub_pem,
            "x25519_public_key": self.identity.x25519_public_key,
            "alias": self._manifest.alias if self._manifest else None,
        }
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/register",
            json=body,
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
        model_requirement: Optional[str] = None,
        target_node: Optional[str] = None,
        *,
        payload_uri: Optional[str] = None,
        secret_data: Optional[str] = None,
        expected_output: Optional[dict[str, Any]] = None,
        intent_type: str = "analysis.request",
        intent_priority: Optional[str] = None,
        task_title: Optional[str] = None,
        task_inputs: Optional[dict[str, Any]] = None,
    ) -> dict:
        payload_to_send = payload
        if target_node and bounty == 0.0:
            try:
                payload_to_send = await self._prepare_dm_payload_for_target(payload, target_node)
            except Exception as exc:
                return {
                    "status_code": 400,
                    "json": {"status": "error", "detail": f"DM privacy policy blocked send: {exc}"},
                }
        body = build_task_envelope(
            self.node_id,
            payload_to_send,
            bounty,
            intent_type=intent_type,
            intent_priority=intent_priority,
            target_node=target_node,
            target_capability=model_requirement,
            expected_output=expected_output,
            task_title=task_title,
            task_inputs=task_inputs,
            payload_uri=payload_uri,
            secret_data=secret_data,
        )
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

    @staticmethod
    def _repo_audit_instructions(
        repo_url: str,
        *,
        audit_type: str,
        ref: Optional[str],
        max_findings: int,
        artifact_preference: str,
    ) -> str:
        lines = [
            f"Run a {audit_type} repo audit for {repo_url}.",
            f"Return at most {max_findings} high-signal findings ranked by developer impact.",
            "Keep the inline summary concise and evidence-backed.",
        ]
        if ref:
            lines.insert(1, f"Audit the repo state at ref {ref}.")
        if artifact_preference == "inline_only":
            lines.append("Return the audit inline without relying on external artifacts.")
        elif artifact_preference == "artifact_preferred":
            lines.append("Use an external artifact for the full report when needed and include its URI.")
        else:
            lines.append("Return an inline summary first and include a result URI only when the report is large.")
        return " ".join(lines)

    async def submit_repo_audit(
        self,
        repo_url: str,
        *,
        audit_type: str = "full_repo_audit",
        ref: Optional[str] = None,
        max_findings: int = 5,
        inline_summary_max_chars: int = 6000,
        artifact_preference: str = "inline_first",
        bounty: float = 0.0,
        target_node: Optional[str] = None,
        target_capability: str = "repo_audit",
    ) -> dict:
        normalized_repo_url = str(repo_url or "").strip()
        if not normalized_repo_url:
            raise ValueError("repo_url must be a non-empty string")
        normalized_audit_type = str(audit_type or "").strip() or "full_repo_audit"
        if max_findings < 1:
            raise ValueError("max_findings must be at least 1")
        if inline_summary_max_chars < 500:
            raise ValueError("inline_summary_max_chars must be at least 500")
        normalized_artifact_preference = str(artifact_preference or "").strip().lower() or "inline_first"
        if normalized_artifact_preference not in {"inline_only", "inline_first", "artifact_preferred"}:
            raise ValueError("artifact_preference must be inline_only, inline_first, or artifact_preferred")

        instructions = self._repo_audit_instructions(
            normalized_repo_url,
            audit_type=normalized_audit_type,
            ref=ref,
            max_findings=max_findings,
            artifact_preference=normalized_artifact_preference,
        )
        task_inputs = {
            "repo_audit": {
                "repo_url": normalized_repo_url,
                "audit_type": normalized_audit_type,
                "max_findings": max_findings,
                "artifact_preference": normalized_artifact_preference,
                "inline_summary_max_chars": inline_summary_max_chars,
            }
        }
        if ref:
            task_inputs["repo_audit"]["ref"] = str(ref).strip()
        expected_output = {
            "result_type": "repo_audit_result",
            "format": "json",
            "artifact_allowed": normalized_artifact_preference != "inline_only",
            "inline_summary_max_chars": inline_summary_max_chars,
        }
        task_title = f"Repo audit: {normalized_repo_url}"
        return await self.submit_task(
            instructions,
            bounty,
            model_requirement=target_capability,
            target_node=target_node,
            expected_output=expected_output,
            intent_type="repo_audit.request",
            intent_priority="high",
            task_title=task_title,
            task_inputs=task_inputs,
        )

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
        task_title: Optional[str] = None,
        session_safety: Optional[dict[str, Any]] = None,
        governance: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
    ) -> dict[str, Any]:
        message_uuid = message_id or str(uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        if turn_index is not None and turn_index < 1:
            raise ValueError("turn_index must be at least 1")
        task_payload: dict[str, Any] = {
            "instructions": message,
            "expected_output": {"result_type": result_type},
        }
        if title or task_title:
            task_payload["title"] = title or task_title
        inputs: dict[str, Any] = dict(task_inputs or {})
        normalized_session_safety = self.build_session_safety_metadata(**session_safety) if session_safety else {}
        if normalized_session_safety and "started_at_ms" not in normalized_session_safety:
            normalized_session_safety["started_at_ms"] = timestamp_ms
        if normalized_session_safety:
            inputs["session_safety"] = normalized_session_safety
        normalized_governance = self._normalize_governance_input(governance) if governance else None
        if normalized_governance:
            inputs["governance"] = normalized_governance
        if expected_output_must_include:
            task_payload["expected_output"]["must_include"] = list(expected_output_must_include)
        if inputs:
            task_payload["inputs"] = inputs
        if constraints:
            task_payload["constraints"] = dict(constraints)
        envelope: dict[str, Any] = {
            "spec_version": INTERBOT_SPEC_VERSION,
            "message_id": message_uuid,
            "trace_id": trace_id or message_uuid,
            "timestamp_ms": timestamp_ms,
            "source": {
                "node_id": self.node_id,
                "alias": self._manifest.alias if self._manifest and self._manifest.alias else None,
            },
            "target": {
                "node_id": target_node,
                "alias": target_alias,
            },
            "conversation": {
                "context_id": context_id or message_uuid,
                "reply_to_task_id": reply_to_task_id,
                "reply_to_message_id": reply_to_message_id,
                "turn_type": turn_type,
                **({"turn_index": turn_index} if turn_index is not None else {}),
            },
            "intent": {"type": intent_type, "priority": priority},
            "task": task_payload,
            "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
            "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
        }
        if human_note:
            envelope["human_note"] = human_note
        return envelope

    async def _submit_interbot_envelope(self, envelope: dict[str, Any]) -> dict:
        target = envelope.get("target") if isinstance(envelope.get("target"), dict) else {}
        target_node = target.get("node_id")
        if not isinstance(target_node, str) or not target_node:
            raise ValueError("inter-bot envelope is missing target.node_id")

        intent = envelope.get("intent") if isinstance(envelope.get("intent"), dict) else {}
        intent_type = intent.get("type")
        if not isinstance(intent_type, str) or not intent_type.strip():
            intent_type = "chat.request"
        intent_priority = intent.get("priority")
        if not isinstance(intent_priority, str) or not intent_priority.strip():
            intent_priority = None

        return await self.submit_task(
            json.dumps(envelope),
            0.0,
            target_node=target_node,
            intent_type=intent_type,
            intent_priority=intent_priority,
        )

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
        session_safety: Optional[dict[str, Any]] = None,
        governance: Optional[dict[str, Any]] = None,
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
            result_type=result_type,
            title=title,
            task_inputs=task_inputs,
            expected_output_must_include=expected_output_must_include,
            constraints=constraints,
            human_note=human_note,
            message_id=message_id,
            trace_id=trace_id,
            session_safety=session_safety,
            governance=governance,
            turn_index=turn_index,
        )
        response = await self._submit_interbot_envelope(envelope)
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
        next_turn_index = self._derive_reply_turn_index(inbound_message)
        return self.build_interbot_message(
            reply_text,
            str(source["node_id"]),
            target_alias=source.get("alias") if isinstance(source.get("alias"), str) else None,
            intent_type=intent_type or self._default_reply_intent_type(
                inbound_intent.get("type") if isinstance(inbound_intent, dict) else None
            ),
            priority=priority or inbound_priority,
            context_id=conversation.get("context_id") if isinstance(conversation, dict) else None,
            reply_to_task_id=inbound_task_id,
            reply_to_message_id=inbound_message.get("message_id") if isinstance(inbound_message.get("message_id"), str) else None,
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
        target = envelope.get("target") if isinstance(envelope.get("target"), dict) else {}
        target_node = target.get("node_id")
        if not isinstance(target_node, str) or not target_node:
            raise ValueError("reply envelope is missing target.node_id")
        response = await self._submit_interbot_envelope(envelope)
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
        session_safety: Optional[dict[str, Any]] = None,
        governance: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
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
            session_safety=session_safety,
            governance=governance,
            turn_index=turn_index,
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
        session_safety: Optional[dict[str, Any]] = None,
        governance: Optional[dict[str, Any]] = None,
        turn_index: Optional[int] = None,
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
            session_safety=session_safety,
            governance=governance,
            turn_index=turn_index,
        )
        response = await self._submit_interbot_envelope(envelope)
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
        response = await self._submit_interbot_envelope(envelope)
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
        response = await self._submit_interbot_envelope(envelope)
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

        governance_payload = self.build_governance_metadata(
            classification="approval_required",
            reason=f"human approval required for {normalized_decision_type}",
            disclosure_scope=[normalized_decision_type],
            approval_status="pending",
        )

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
            governance=governance_payload,
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
        response = await self._submit_interbot_envelope(envelope)
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

    @classmethod
    def parse_interbot_payload(cls, payload_text: str) -> Optional[dict[str, Any]]:
        try:
            parsed = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("spec_version") not in ("mep.interbot.v1", "mep.execution-bridge.v1"):
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
    def build_governance_metadata(
        cls,
        *,
        classification: str,
        reason: str,
        disclosure_scope: Optional[list[str]] = None,
        redaction_applied: bool = False,
        approval_status: Optional[str] = None,
        approval_context_id: Optional[str] = None,
        approved_by: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_classification = classification.strip().lower() if isinstance(classification, str) else ""
        if normalized_classification not in GOVERNANCE_CLASSIFICATIONS:
            raise ValueError(f"unsupported governance classification: {classification}")
        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason:
            raise ValueError("governance reason must be non-empty")
        normalized: dict[str, Any] = {
            "classification": normalized_classification,
            "reason": normalized_reason,
            "redaction_applied": bool(redaction_applied),
        }
        normalized_scope = cls._normalize_string_list(disclosure_scope)
        if normalized_scope:
            normalized["disclosure_scope"] = normalized_scope
        if approval_status is not None:
            normalized_status = approval_status.strip().lower() if isinstance(approval_status, str) else ""
            if normalized_status not in GOVERNANCE_APPROVAL_STATUSES:
                raise ValueError(f"unsupported governance approval status: {approval_status}")
            approval_payload: dict[str, Any] = {"status": normalized_status}
            if isinstance(approval_context_id, str) and approval_context_id.strip():
                approval_payload["context_id"] = approval_context_id.strip()
            if isinstance(approved_by, str) and approved_by.strip():
                approval_payload["approved_by"] = approved_by.strip()
            normalized["approval"] = approval_payload
        return normalized

    @classmethod
    def _normalize_governance_input(cls, governance: dict[str, Any]) -> dict[str, Any]:
        approval = governance.get("approval") if isinstance(governance.get("approval"), dict) else {}
        return cls.build_governance_metadata(
            classification=governance.get("classification"),
            reason=governance.get("reason"),
            disclosure_scope=governance.get("disclosure_scope"),
            redaction_applied=bool(governance.get("redaction_applied", False)),
            approval_status=approval.get("status"),
            approval_context_id=approval.get("context_id"),
            approved_by=approval.get("approved_by"),
        )

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

    @classmethod
    def extract_governance_metadata(cls, payload_text: str) -> Optional[dict[str, Any]]:
        parsed = cls.parse_interbot_payload(payload_text)
        if not parsed:
            return None
        task = parsed.get("task")
        if not isinstance(task, dict):
            return None
        inputs = task.get("inputs")
        if not isinstance(inputs, dict):
            return None
        governance = inputs.get("governance")
        if not isinstance(governance, dict):
            return None
        try:
            return cls._normalize_governance_input(governance)
        except ValueError:
            return None

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
            uri = f"{self.ws_url}/ws/{self.node_id}?timestamp={ts}&signature={sig}"
            try:
                async with ws_connect(uri) as ws:
                    self._active_ws = ws
                    heartbeat_task: Optional[asyncio.Task] = None
                    if self.heartbeat_seconds > 0:
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        for context_id in tuple(self._live_call_contexts):
                            await ws.send(json.dumps({"event": "call.resume", "context_id": context_id}))
                        while not self._stop.is_set():
                            msg = await ws.recv()
                            data = json.loads(msg)
                            event = data.get("event")
                            context_id = data.get("context_id")
                            if event == "call.ping" and isinstance(context_id, str) and context_id:
                                await ws.send(json.dumps({"event": "call.pong", "context_id": context_id}))
                            elif event == "call.accepted" and isinstance(context_id, str) and context_id:
                                self._live_call_contexts.add(context_id)
                            elif event in {"call.hangup", "call.declined", "call.timeout", "call.rejected", "call.cancelled"}:
                                if isinstance(context_id, str):
                                    self._live_call_contexts.discard(context_id)
                            if data.get("event") == "task_result":
                                task_data = data.get("data", {})
                                if isinstance(task_data, dict) and isinstance(task_data.get("result_payload"), str):
                                    task_data["result_payload"] = self._maybe_decrypt_dm_payload(task_data["result_payload"])
                                await on_result(data["data"])
                            elif on_event is not None:
                                await on_event(data)
                    finally:
                        self._active_ws = None
                        if heartbeat_task:
                            heartbeat_task.cancel()
                            await asyncio.gather(heartbeat_task, return_exceptions=True)
            except Exception:
                self._active_ws = None
                await asyncio.sleep(2)

    async def send_ws_event(self, payload: dict[str, Any]) -> bool:
        if self._active_ws is None:
            return False
        await self._active_ws.send(json.dumps(payload))
        event = payload.get("event")
        context_id = payload.get("context_id")
        if event == "call.accept" and isinstance(context_id, str) and context_id:
            self._live_call_contexts.add(context_id)
        elif event in {"call.hangup", "call.decline", "call.cancel"} and isinstance(context_id, str):
            self._live_call_contexts.discard(context_id)
        return True

    async def _heartbeat_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            await ws.send(json.dumps({"event": "heartbeat", "node_id": self.node_id, "ts": int(time.time())}))

    def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

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
            if require_encrypted:
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
