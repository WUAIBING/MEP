import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from clients.shared.identity import MEPIdentity
from node.task_envelope import build_task_envelope


SUPPORTED_GITHUB_EVENTS = {"issue_comment", "pull_request", "pull_request_review_comment"}
DEFAULT_TRIGGER_VERBS = {
    "review": "code.review.request",
    "analyze": "analysis.request",
    "check": "code.review.request",
    "comment": "code.review.comment",
    "approve": "code.review.approve",
    "triage": "issue.triage.request",
}
DEFAULT_ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
BRIDGE_OUTPUT_MARKER = "<!-- mep-bridge:output"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item and item.strip()]


def _normalize_alias_key(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"[\s_-]+", " ", value).strip().lower()


def _parse_json_str_dict_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid {name}: expected JSON object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RuntimeError(f"Invalid {name}: expected string keys and values")
        normalized_key = key.strip()
        normalized_value = item.strip()
        if normalized_key and normalized_value:
            result[normalized_key] = normalized_value
    return result


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


@dataclass(slots=True)
class BridgeConfig:
    hub_url: str
    key_path: str
    sqlite_path: str
    webhook_secret: str
    github_token: Optional[str]
    github_writeback_aliases: set[str]
    github_writeback_login: Optional[str]
    github_tokens_by_alias: dict[str, str]
    github_logins_by_alias: dict[str, str]
    target_node_id: str
    target_alias: str
    trigger_aliases: list[str]
    alias_map: dict[str, str]
    public_base_url: str
    status_secret: str
    status_token_lifetime_seconds: int
    dedup_ttl_hours: int
    coalesce_window_seconds: float
    coalesce_max_buffer_size: int
    allowed_repos: set[str]
    maintainer_only: bool
    allowed_associations: set[str]
    human_only_triggers: bool
    trusted_bot_logins: set[str]
    bridge_source_alias: str
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    compact_telegram_updates: bool

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        # --- Multi-target alias map ---
        alias_map_raw = os.getenv("MEP_BRIDGE_ALIAS_MAP", "").strip()
        if alias_map_raw:
            try:
                alias_map = json.loads(alias_map_raw)
                if not isinstance(alias_map, dict) or not alias_map:
                    raise ValueError("MEP_BRIDGE_ALIAS_MAP must be a non-empty JSON dict")
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(f"Invalid MEP_BRIDGE_ALIAS_MAP: {exc}") from exc
        else:
            alias_map = {}

        target_alias = os.getenv("MEP_BRIDGE_TARGET_ALIAS", "Hub Sentinel").strip() or "Hub Sentinel"
        target_node_id = os.getenv("MEP_BRIDGE_TARGET_NODE_ID", "").strip()

        # Populate alias_map from legacy single-target vars if no explicit map
        if not alias_map and target_node_id:
            alias_map[target_alias] = target_node_id

        # trigger_aliases = all keys in alias_map, supplemented by env
        trigger_aliases = _split_csv(os.getenv("MEP_BRIDGE_TRIGGER_ALIASES", ""))
        for alias in alias_map:
            if alias not in trigger_aliases:
                trigger_aliases.append(alias)
        if not trigger_aliases and target_alias:
            trigger_aliases = [target_alias]

        allowed_repos = set(_split_csv(os.getenv("GITHUB_ALLOWED_REPOS", "")))
        allowed_associations = set(
            item.upper() for item in _split_csv(os.getenv("MEP_BRIDGE_ALLOWED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR"))
        ) or set(DEFAULT_ALLOWED_ASSOCIATIONS)
        trusted_bot_logins = set(
            item.lower() for item in _split_csv(os.getenv("MEP_BRIDGE_TRUSTED_BOT_LOGINS", ""))
        )
        github_writeback_aliases = set(_split_csv(os.getenv("MEP_BRIDGE_GITHUB_WRITEBACK_ALIASES", "")))
        github_tokens_by_alias = _parse_json_str_dict_env("MEP_BRIDGE_GITHUB_TOKENS_BY_ALIAS")
        github_logins_by_alias = _parse_json_str_dict_env("MEP_BRIDGE_GITHUB_LOGINS_BY_ALIAS")
        return cls(
            hub_url=os.getenv("MEP_HUB_URL", "").rstrip("/"),
            key_path=os.getenv(
                "MEP_BRIDGE_KEY_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_identity.pem"),
            ),
            sqlite_path=os.getenv(
                "MEP_BRIDGE_SQLITE_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "github_bridge.db"),
            ),
            webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
            github_token=(os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN") or "").strip() or None,
            github_writeback_aliases=github_writeback_aliases,
            github_writeback_login=(os.getenv("MEP_BRIDGE_GITHUB_WRITEBACK_LOGIN") or "").strip() or None,
            github_tokens_by_alias=github_tokens_by_alias,
            github_logins_by_alias=github_logins_by_alias,
            target_node_id=target_node_id,
            target_alias=target_alias,
            trigger_aliases=trigger_aliases,
            alias_map=alias_map,
            public_base_url=os.getenv("MEP_BRIDGE_PUBLIC_BASE_URL", "http://localhost:8787").rstrip("/"),
            status_secret=os.getenv("MEP_BRIDGE_STATUS_SECRET", ""),
            status_token_lifetime_seconds=max(
                60, int(os.getenv("MEP_BRIDGE_STATUS_TOKEN_LIFETIME_SECONDS", "1800"))
            ),
            dedup_ttl_hours=max(1, int(os.getenv("MEP_BRIDGE_DEDUP_TTL_HOURS", "72"))),
            coalesce_window_seconds=max(
                0.0, float(os.getenv("MEP_BRIDGE_COALESCE_WINDOW_SECONDS", "10"))
            ),
            coalesce_max_buffer_size=max(
                1, int(os.getenv("MEP_BRIDGE_COALESCE_MAX_BUFFER_SIZE", "50"))
            ),
            allowed_repos=allowed_repos,
            maintainer_only=_env_bool("MEP_BRIDGE_MAINTAINER_ONLY", True),
            allowed_associations=allowed_associations,
            human_only_triggers=_env_bool("MEP_BRIDGE_HUMAN_ONLY_TRIGGERS", True),
            trusted_bot_logins=trusted_bot_logins,
            bridge_source_alias=os.getenv("MEP_BRIDGE_SOURCE_ALIAS", "GitHub Bridge").strip() or "GitHub Bridge",
            telegram_bot_token=(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None,
            telegram_chat_id=(os.getenv("TELEGRAM_CHAT_ID") or "").strip() or None,
            compact_telegram_updates=_env_bool("MEP_BRIDGE_TELEGRAM_COMPACT", True),
        )


@dataclass(slots=True)
class NormalizedGitHubEvent:
    delivery_id: str
    source_event: str
    source_action: str
    repo_full_name: str
    entity_type: str
    number: int
    title: str
    url: str
    actor_login: str
    author_association: str
    context_id: str
    imperative_verb: str
    intent_type: str
    instructions: str
    raw_trigger_text: str
    github_inputs: dict[str, Any] = field(default_factory=dict)
    event_sequence: int = 0
    bridge_id: str = ""
    coalesced_delivery_ids: list[str] = field(default_factory=list)
    coalesced_actions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TriggerMatch:
    alias: str
    verb: str
    intent_type: str
    node_id: str


@dataclass(slots=True)
class PendingContext:
    context_id: str
    bridge_id: str
    event: NormalizedGitHubEvent
    targets: list[TriggerMatch] = field(default_factory=list)
    first_seen: float = 0.0
    flush_task: Optional[asyncio.Task] = None


class BridgeStatusUpdate(BaseModel):
    bridge_id: str
    status: str = Field(..., min_length=1, max_length=64)
    context_id: Optional[str] = None
    target_node_id: Optional[str] = None
    task_id: Optional[str] = None
    action: Optional[str] = None
    timestamp_ms: Optional[int] = None
    detail: Optional[str] = None


class BridgeRegistrationPendingApprovalError(RuntimeError):
    def __init__(self, node_id: str):
        super().__init__(
            "Bridge node "
            f"{node_id} is pending admin approval on the hub. "
            "Approve the registration before using the bridge."
        )
        self.node_id = node_id


class BridgeStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_deliveries (
                delivery_id TEXT PRIMARY KEY,
                bridge_id TEXT NOT NULL,
                context_id TEXT NOT NULL,
                source_event TEXT NOT NULL,
                source_action TEXT NOT NULL,
                repo_full_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                received_at REAL NOT NULL,
                raw_event TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_executions (
                bridge_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                repo_full_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                target_alias TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                imperative_verb TEXT NOT NULL,
                intent_type TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                status TEXT NOT NULL,
                task_id TEXT,
                action TEXT,
                telegram_message_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_context_sequences (
                context_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_bridge_deliveries_context_received
            ON bridge_deliveries (context_id, received_at)
            """
        )
        cursor.execute("PRAGMA table_info(bridge_executions)")
        execution_columns = {str(row["name"]) for row in cursor.fetchall()}
        if "target_alias" not in execution_columns:
            cursor.execute(
                """
                ALTER TABLE bridge_executions
                ADD COLUMN target_alias TEXT NOT NULL DEFAULT ''
                """
            )
        conn.commit()
        conn.close()

    def cleanup_expired_deliveries(self, ttl_hours: int) -> None:
        cutoff = time.time() - max(1, ttl_hours) * 3600.0
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bridge_deliveries WHERE received_at < ?", (cutoff,))
        conn.commit()
        conn.close()

    def delivery_exists(self, delivery_id: str) -> bool:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM bridge_deliveries WHERE delivery_id = ?", (delivery_id,))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists

    def next_event_sequence(self, context_id: str) -> int:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_sequence FROM bridge_context_sequences WHERE context_id = ?",
            (context_id,),
        )
        row = cursor.fetchone()
        next_sequence = int(row["last_sequence"]) + 1 if row else 1
        cursor.execute(
            """
            INSERT INTO bridge_context_sequences (context_id, last_sequence)
            VALUES (?, ?)
            ON CONFLICT(context_id) DO UPDATE SET last_sequence = excluded.last_sequence
            """,
            (context_id, next_sequence),
        )
        conn.commit()
        conn.close()
        return next_sequence

    def create_execution(
        self,
        event: NormalizedGitHubEvent,
        bridge_id: str,
        target_node_id: str,
        *,
        target_alias: str,
        event_sequence: Optional[int] = None,
    ) -> int:
        now = time.time()
        if event_sequence is None:
            event_sequence = self.next_event_sequence(event.context_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bridge_executions (
                bridge_id, context_id, repo_full_name, issue_number, entity_type,
                target_alias, target_node_id, imperative_verb, intent_type, event_sequence,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bridge_id,
                event.context_id,
                event.repo_full_name,
                event.number,
                event.entity_type,
                target_alias,
                target_node_id,
                event.imperative_verb,
                event.intent_type,
                event_sequence,
                "buffered",
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return event_sequence

    def record_delivery(
        self,
        *,
        delivery_id: str,
        bridge_id: str,
        context_id: str,
        source_event: str,
        source_action: str,
        repo_full_name: str,
        issue_number: int,
        status: str,
        raw_event: dict[str, Any],
    ) -> None:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bridge_deliveries (
                delivery_id, bridge_id, context_id, source_event, source_action,
                repo_full_name, issue_number, status, received_at, raw_event
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                bridge_id,
                context_id,
                source_event,
                source_action,
                repo_full_name,
                issue_number,
                status,
                time.time(),
                json.dumps(raw_event),
            ),
        )
        conn.commit()
        conn.close()

    def update_execution(
        self,
        bridge_id: str,
        *,
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        action: Optional[str] = None,
        telegram_message_id: Optional[str] = None,
    ) -> None:
        assignments = []
        params: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if task_id is not None:
            assignments.append("task_id = ?")
            params.append(task_id)
        if action is not None:
            assignments.append("action = ?")
            params.append(action)
        if telegram_message_id is not None:
            assignments.append("telegram_message_id = ?")
            params.append(str(telegram_message_id))
        assignments.append("updated_at = ?")
        params.append(time.time())
        params.append(bridge_id)
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE bridge_executions SET {', '.join(assignments)} WHERE bridge_id = ?",
            params,
        )
        conn.commit()
        conn.close()

    def get_execution(self, bridge_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bridge_executions WHERE bridge_id = ?", (bridge_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None


class DefaultMEPSubmissionClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.identity = MEPIdentity(config.key_path)
        self.node_id = self.identity.node_id
        self.session = requests.Session()
        self._registered = False

    def ensure_registered(self) -> None:
        if self._registered:
            return
        response = self.session.post(
            f"{self.config.hub_url}/register",
            json={"pubkey": self.identity.pub_pem, "alias": self.config.bridge_source_alias},
            timeout=10,
        )
        response.raise_for_status()
        payload: dict[str, Any] = {}
        try:
            decoded = response.json()
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            payload = decoded
        if str(payload.get("status") or "").strip().lower() == "pending":
            raise BridgeRegistrationPendingApprovalError(self.node_id)
        self._registered = True

    def submit_structured_dm(self, envelope: dict[str, Any], target_node_id: str, intent_type: str) -> dict[str, Any]:
        self.ensure_registered()
        outer_task = build_task_envelope(
            self.node_id,
            json.dumps(envelope),
            0.0,
            intent_type=intent_type,
            target_node=target_node_id,
        )
        payload_str = json.dumps(outer_task)
        headers = self.identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        response = self.session.post(
            f"{self.config.hub_url}/tasks/submit",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        return {"status_code": response.status_code, "json": response.json()}


class TelegramNotifier:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.session = requests.Session()

    def send_or_edit(self, text: str, message_id: Optional[str] = None) -> Optional[str]:
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return message_id
        base = f"https://api.telegram.org/bot{self.config.telegram_bot_token}"
        if self.config.compact_telegram_updates and message_id:
            response = self.session.post(
                f"{base}/editMessageText",
                json={
                    "chat_id": self.config.telegram_chat_id,
                    "message_id": int(message_id),
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            response.raise_for_status()
            return str(message_id)
        response = self.session.post(
            f"{base}/sendMessage",
            json={
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and result.get("message_id") is not None:
            return str(result["message_id"])
        return message_id


class GitHubToMEPBridgeService:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        store: Optional[BridgeStore] = None,
        submission_client: Optional[Any] = None,
        notifier: Optional[Any] = None,
        github_session: Optional[Any] = None,
    ):
        self.config = config
        self.store = store or BridgeStore(config.sqlite_path)
        self.submission_client = submission_client or DefaultMEPSubmissionClient(config)
        self.notifier = notifier or TelegramNotifier(config)
        self.github_session = github_session or requests.Session()
        self._pending_lock = asyncio.Lock()
        self._pending_by_context: dict[str, PendingContext] = {}

    def _require_runtime_config(self) -> None:
        missing = []
        if not self.config.webhook_secret:
            missing.append("GITHUB_WEBHOOK_SECRET")
        if not self.config.hub_url:
            missing.append("MEP_HUB_URL")
        if not self.config.target_node_id and not self.config.alias_map:
            missing.append("MEP_BRIDGE_TARGET_NODE_ID")
        if not self.config.status_secret:
            missing.append("MEP_BRIDGE_STATUS_SECRET")
        if missing:
            raise HTTPException(
                status_code=500,
                detail=f"Bridge configuration missing: {', '.join(missing)}",
            )

    def verify_github_signature(self, body: bytes, signature_header: Optional[str]) -> None:
        self._require_runtime_config()
        if not signature_header or not signature_header.startswith("sha256="):
            raise HTTPException(status_code=401, detail="Missing GitHub signature")
        expected = hmac.new(
            self.config.webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        provided = signature_header.split("=", 1)[1].strip()
        if not hmac.compare_digest(expected, provided):
            raise HTTPException(status_code=401, detail="Invalid GitHub signature")

    async def handle_github_webhook(
        self,
        *,
        delivery_id: str,
        github_event: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_runtime_config()
        if github_event not in SUPPORTED_GITHUB_EVENTS:
            return {"status": "ignored", "reason": f"unsupported_event:{github_event}"}
        self.store.cleanup_expired_deliveries(self.config.dedup_ttl_hours)
        if self.store.delivery_exists(delivery_id):
            return {"status": "duplicate", "delivery_id": delivery_id}
        normalized = self._normalize_github_event(delivery_id, github_event, payload)
        if normalized is None:
            return {"status": "ignored", "delivery_id": delivery_id, "reason": "non_actionable"}
        result = await self._buffer_event(normalized, payload)
        result["delivery_id"] = delivery_id
        return result

    async def _buffer_event(self, event: NormalizedGitHubEvent, raw_event: dict[str, Any]) -> dict[str, Any]:
        flush_context_id: Optional[str] = None
        async with self._pending_lock:
            pending = self._pending_by_context.get(event.context_id)
            if pending is not None:
                existing_targets = {
                    _normalize_alias_key(target.alias): target
                    for target in (pending.targets or [])
                }
                pending.event = self._merge_events(pending.event, event)
                for target in self._get_targets_for_event(pending.event):
                    existing_targets[_normalize_alias_key(target.alias)] = target
                pending.targets = list(existing_targets.values())
                if not pending.targets and self.config.target_node_id:
                    pending.targets = [
                        TriggerMatch(
                            alias=self.config.target_alias,
                            verb=pending.event.imperative_verb,
                            intent_type=pending.event.intent_type,
                            node_id=self.config.target_node_id,
                        )
                    ]
                self.store.record_delivery(
                    delivery_id=event.delivery_id,
                    bridge_id=pending.bridge_id,
                    context_id=pending.context_id,
                    source_event=event.source_event,
                    source_action=event.source_action,
                    repo_full_name=event.repo_full_name,
                    issue_number=event.number,
                    status="buffered",
                    raw_event=raw_event,
                )
                return {
                    "status": "buffered",
                    "bridge_id": pending.bridge_id,
                    "context_id": pending.context_id,
                    "coalesced": True,
                }

            targets = self._get_targets_for_event(event)
            if not targets:
                targets = [TriggerMatch(
                    alias=self.config.target_alias,
                    verb=event.imperative_verb,
                    intent_type=event.intent_type,
                    node_id=self.config.target_node_id,
                )]
            bridge_id = f"br-{secrets.token_hex(8)}"
            event_sequence = self.store.next_event_sequence(event.context_id)
            event.bridge_id = bridge_id
            event.event_sequence = event_sequence
            if not event.coalesced_delivery_ids:
                event.coalesced_delivery_ids = [event.delivery_id]
            if not event.coalesced_actions:
                event.coalesced_actions = [event.source_action]
            self.store.record_delivery(
                delivery_id=event.delivery_id,
                bridge_id=bridge_id,
                context_id=event.context_id,
                source_event=event.source_event,
                source_action=event.source_action,
                repo_full_name=event.repo_full_name,
                issue_number=event.number,
                status="buffered",
                raw_event=raw_event,
            )
            pending = PendingContext(
                context_id=event.context_id,
                bridge_id=bridge_id,
                event=event,
                targets=targets,
                first_seen=time.time(),
            )
            pending.flush_task = asyncio.create_task(self._flush_after_delay(event.context_id))
            self._pending_by_context[event.context_id] = pending

            if len(self._pending_by_context) > self.config.coalesce_max_buffer_size:
                oldest = min(self._pending_by_context.values(), key=lambda item: item.first_seen)
                if oldest.context_id != event.context_id:
                    flush_context_id = oldest.context_id

        if flush_context_id:
            await self._flush_context(flush_context_id)
        return {
            "status": "buffered",
            "bridge_id": bridge_id,
            "context_id": event.context_id,
            "coalesced": False,
            "event_sequence": event.event_sequence,
        }

    def _merge_events(
        self,
        existing: NormalizedGitHubEvent,
        incoming: NormalizedGitHubEvent,
    ) -> NormalizedGitHubEvent:
        existing.source_action = incoming.source_action or existing.source_action
        existing.actor_login = incoming.actor_login or existing.actor_login
        existing.author_association = incoming.author_association or existing.author_association
        existing.imperative_verb = incoming.imperative_verb or existing.imperative_verb
        existing.intent_type = incoming.intent_type or existing.intent_type
        existing.instructions = incoming.instructions or existing.instructions
        existing.raw_trigger_text = incoming.raw_trigger_text or existing.raw_trigger_text
        if incoming.delivery_id not in existing.coalesced_delivery_ids:
            existing.coalesced_delivery_ids.append(incoming.delivery_id)
        if incoming.source_action and incoming.source_action not in existing.coalesced_actions:
            existing.coalesced_actions.append(incoming.source_action)
        return existing

    async def _flush_after_delay(self, context_id: str) -> None:
        try:
            await asyncio.sleep(self.config.coalesce_window_seconds)
            await self._flush_context(context_id)
        except asyncio.CancelledError:
            return

    async def _flush_context(self, context_id: str) -> None:
        async with self._pending_lock:
            pending = self._pending_by_context.pop(context_id, None)
        if pending is None:
            return
        if pending.flush_task and pending.flush_task is not asyncio.current_task():
            pending.flush_task.cancel()

        targets = pending.targets or []
        if not targets:
            targets = [TriggerMatch(
                alias=self.config.target_alias,
                verb=pending.event.imperative_verb,
                intent_type=pending.event.intent_type,
                node_id=self.config.target_node_id,
            )]

        single_target = len(targets) == 1
        for target in targets:
            target_bridge_id = pending.bridge_id if single_target else (
                f"{pending.bridge_id}-{target.alias.replace(' ', '-').lower()}"
            )
            self.store.create_execution(
                pending.event,
                target_bridge_id,
                target.node_id,
                target_alias=target.alias,
                event_sequence=pending.event.event_sequence,
            )
            self.store.update_execution(target_bridge_id, status="submitting")
            envelope = self._build_interbot_envelope(
                pending.event, target_node_id=target.node_id,
                target_alias=target.alias, bridge_id=target_bridge_id,
            )
            try:
                response = await asyncio.to_thread(
                    self.submission_client.submit_structured_dm,
                    envelope,
                    target.node_id,
                    target.intent_type,
                )
            except Exception as exc:  # noqa: BLE001
                self.store.update_execution(
                    target_bridge_id, status="submit_failed", action="error"
                )
                await self._notify_status(
                    target_bridge_id,
                    self._render_status_text(
                        pending.event, "submit_failed",
                        target_alias=target.alias,
                        target_node_id=target.node_id,
                        detail=str(exc),
                    ),
                )
                continue

            status_code = int(response.get("status_code") or 500)
            payload = response.get("json") if isinstance(response, dict) else None
            if status_code >= 400 or not isinstance(payload, dict):
                detail = json.dumps(payload) if payload is not None else f"http_{status_code}"
                self.store.update_execution(
                    target_bridge_id, status="submit_failed", action="error"
                )
                await self._notify_status(
                    target_bridge_id,
                    self._render_status_text(
                        pending.event, "submit_failed",
                        target_alias=target.alias,
                        target_node_id=target.node_id,
                        detail=detail,
                    ),
                )
                continue

            task_id = payload.get("task_id")
            execution_status = str(payload.get("status") or "submitted")
            self.store.update_execution(
                target_bridge_id,
                status=execution_status,
                task_id=str(task_id) if task_id else None,
            )
            await self._notify_status(
                target_bridge_id,
                self._render_status_text(
                    pending.event,
                    execution_status,
                    task_id=str(task_id) if task_id else None,
                    target_alias=target.alias,
                    target_node_id=target.node_id,
                ),
            )

    async def _notify_status(self, bridge_id: str, text: str) -> None:
        execution = self.store.get_execution(bridge_id)
        message_id = execution.get("telegram_message_id") if execution else None
        new_message_id = await asyncio.to_thread(self.notifier.send_or_edit, text, message_id)
        if new_message_id and new_message_id != message_id:
            self.store.update_execution(bridge_id, telegram_message_id=str(new_message_id))

    def _normalize_github_event(
        self,
        delivery_id: str,
        github_event: str,
        payload: dict[str, Any],
    ) -> Optional[NormalizedGitHubEvent]:
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            return None
        repo_full_name = str(repository.get("full_name") or "").strip()
        if not repo_full_name:
            return None
        if self.config.allowed_repos and repo_full_name not in self.config.allowed_repos:
            return None

        action = str(payload.get("action") or "").strip() or "unknown"
        entity_type = "pr"
        subject = payload.get("pull_request")
        issue = payload.get("issue")
        if not isinstance(subject, dict) and isinstance(issue, dict):
            subject = issue
            entity_type = "pr" if isinstance(issue.get("pull_request"), dict) else "issue"
        elif not isinstance(subject, dict):
            return None

        number = payload.get("number", subject.get("number"))
        if not isinstance(number, int):
            return None
        title = str(subject.get("title") or f"{repo_full_name}#{number}")
        url = str(
            subject.get("html_url")
            or (payload.get("comment") or {}).get("html_url")
            or f"https://github.com/{repo_full_name}"
        )

        comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else None
        trigger_text = ""
        author_association = ""
        if comment is not None:
            trigger_text = str(comment.get("body") or "")
            author_association = str(comment.get("author_association") or "")
        if not trigger_text:
            trigger_text = str(subject.get("body") or "")
        if not author_association:
            author_association = str(subject.get("author_association") or "")
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        actor_login = str(sender.get("login") or "unknown")
        actor_type = str(sender.get("type") or "")

        if self._contains_bridge_output_marker(trigger_text):
            return None

        if self._is_bot_sender(actor_login, actor_type):
            actor_login_key = actor_login.strip().lower()
            if self.config.human_only_triggers and actor_login_key not in self.config.trusted_bot_logins:
                return None

        if self.config.maintainer_only:
            if author_association.upper() not in self.config.allowed_associations:
                return None

        triggers = self._extract_triggers(trigger_text)
        if not triggers:
            return None
        # Use first trigger as primary; all triggers stored on event via _pending_targets
        first_verb = triggers[0].verb
        first_intent = triggers[0].intent_type
        github_inputs = {
            "repo_full_name": repo_full_name,
            "entity_type": entity_type,
            "number": number,
            "url": url,
            "actor_login": actor_login,
            "author_association": author_association.upper(),
            "delivery_id": delivery_id,
            "source_event": github_event,
            "source_action": action,
            "trigger_verb": first_verb,
        }
        review_context = ""
        if entity_type == "pr":
            review_package = self._fetch_pr_review_package(repo_full_name, number)
            review_context = str(review_package.get("instructions_context") or "")
            for key, value in review_package.items():
                if key != "instructions_context":
                    github_inputs[key] = value

        owner, repo_name = repo_full_name.split("/", 1)
        context_id = f"github-{owner}-{repo_name}-{entity_type}-{number}"
        instructions = self._build_instructions(
            repo_full_name=repo_full_name,
            entity_type=entity_type,
            number=number,
            title=title,
            url=url,
            actor_login=actor_login,
            action=action,
            imperative_verb=first_verb,
            trigger_text=trigger_text,
            review_context=review_context,
        )
        return NormalizedGitHubEvent(
            delivery_id=delivery_id,
            source_event=github_event,
            source_action=action,
            repo_full_name=repo_full_name,
            entity_type=entity_type,
            number=number,
            title=title,
            url=url,
            actor_login=actor_login,
            author_association=author_association.upper(),
            context_id=context_id,
            imperative_verb=first_verb,
            intent_type=first_intent,
            instructions=instructions,
            raw_trigger_text=trigger_text.strip(),
            github_inputs=github_inputs,
            coalesced_delivery_ids=[delivery_id],
            coalesced_actions=[action],
        )

    def _get_targets_for_event(
        self, event: NormalizedGitHubEvent
    ) -> list[TriggerMatch]:
        """Re-extract multi-target triggers from a normalized event's trigger text."""
        return self._extract_triggers(event.raw_trigger_text)

    def _extract_triggers(self, text: str) -> list[TriggerMatch]:
        """Extract ALL actionable @alias verb mentions from text."""
        if not isinstance(text, str) or not text.strip():
            return []
        alias_lookup = {
            alias.strip().lower(): alias
            for alias in self.config.trigger_aliases
            if isinstance(alias, str) and alias.strip()
        }
        if not alias_lookup:
            return []
        alias_pattern = "|".join(
            sorted((re.escape(alias) for alias in alias_lookup.values()), key=len, reverse=True)
        )
        mention_pattern = re.compile(rf"@(?P<alias>{alias_pattern})\b", re.IGNORECASE)
        mention_matches = list(mention_pattern.finditer(text))
        if not mention_matches:
            return []
        matches: list[TriggerMatch] = []
        seen_aliases: set[str] = set()
        mention_index = 0
        while mention_index < len(mention_matches):
            grouped_mentions = [mention_matches[mention_index]]
            cursor = mention_matches[mention_index].end()
            mention_index += 1
            while mention_index < len(mention_matches):
                separator = text[cursor:mention_matches[mention_index].start()]
                if re.fullmatch(r"(?:\s|[,:])+", separator):
                    grouped_mentions.append(mention_matches[mention_index])
                    cursor = mention_matches[mention_index].end()
                    mention_index += 1
                    continue
                break
            verb_match = re.match(
                r"(?:\s+|[,:]\s*)(?P<verb>[a-z][a-z_-]*)\b",
                text[cursor:],
                re.IGNORECASE,
            )
            if not verb_match:
                continue
            verb = verb_match.group("verb").strip().lower()
            intent_type = DEFAULT_TRIGGER_VERBS.get(verb)
            if not intent_type:
                continue
            for mention in grouped_mentions:
                raw_alias = mention.group("alias").strip().lower()
                alias = alias_lookup.get(raw_alias)
                if not alias or raw_alias in seen_aliases:
                    continue
                node_id = self.config.alias_map.get(alias, self.config.target_node_id)
                if not node_id:
                    continue
                matches.append(TriggerMatch(alias=alias, verb=verb, intent_type=intent_type, node_id=node_id))
                seen_aliases.add(raw_alias)
        return matches

    def _extract_trigger(self, text: str) -> Optional[tuple[str, str]]:
        """Legacy single-match method retained for backward-compatible callers."""
        triggers = self._extract_triggers(text)
        if not triggers:
            return None
        first = triggers[0]
        return first.verb, first.intent_type

    def _contains_bridge_output_marker(self, text: str) -> bool:
        return isinstance(text, str) and BRIDGE_OUTPUT_MARKER in text.lower()

    def _is_bot_sender(self, actor_login: str, actor_type: str) -> bool:
        login = actor_login.strip().lower()
        sender_type = actor_type.strip().lower()
        return sender_type == "bot" or login.endswith("[bot]")

    @staticmethod
    def _clip_text(value: str, max_chars: int) -> str:
        text = value.strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    def _github_read_token(self) -> Optional[str]:
        if self.config.github_token:
            return self.config.github_token
        for token in self.config.github_tokens_by_alias.values():
            if token and token.strip():
                return token.strip()
        return None

    def _fetch_pr_review_context(self, repo_full_name: str, number: int) -> str:
        review_package = self._fetch_pr_review_package(repo_full_name, number)
        return str(review_package.get("instructions_context") or "")

    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = path.strip().lower()
        if not lowered:
            return False
        basename = lowered.rsplit("/", 1)[-1]
        return (
            lowered.startswith("tests/")
            or "/tests/" in lowered
            or basename.startswith("test_")
            or basename.endswith("_test.py")
            or ".test." in basename
            or ".spec." in basename
        )

    @staticmethod
    def _derive_risk_tags(paths: list[str]) -> list[str]:
        joined = "\n".join(path.strip().lower() for path in paths if isinstance(path, str))
        if not joined:
            return []
        tag_rules = [
            ("auth", ("auth", "login", "token", "identity", "permission", "oauth", "credential", "secret")),
            ("persistence", ("db", "sqlite", "postgres", "storage", "persist", "migration", "cache")),
            ("concurrency", ("async", "ws", "websocket", "thread", "queue", "lock", "runtime")),
            ("api_contract", ("api", "schema", "protocol", "webhook", "client", "server", "route", "endpoint")),
            ("money_flow", ("payment", "billing", "wallet", "balance", "economics", "bounty", "settlement")),
        ]
        tags: list[str] = []
        for tag, keywords in tag_rules:
            if any(keyword in joined for keyword in keywords):
                tags.append(tag)
        return tags

    def _fetch_pr_review_package(self, repo_full_name: str, number: int) -> dict[str, Any]:
        token = self._github_read_token()
        if not token:
            return {}
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            pr_response = self.github_session.get(
                f"https://api.github.com/repos/{repo_full_name}/pulls/{number}",
                headers=headers,
                timeout=20,
            )
            if pr_response.status_code != 200:
                return {}
            pr_data = pr_response.json()
            files_response = self.github_session.get(
                f"https://api.github.com/repos/{repo_full_name}/pulls/{number}/files?per_page=20",
                headers=headers,
                timeout=20,
            )
            files = files_response.json() if files_response.status_code == 200 else []
        except Exception:
            return {}

        sections: list[str] = []
        pr_body = str(pr_data.get("body") or "").strip()
        if pr_body:
            sections.append("PR description:\n" + self._clip_text(pr_body, 700))

        changed_files = int(pr_data.get("changed_files") or 0)
        additions = int(pr_data.get("additions") or 0)
        deletions = int(pr_data.get("deletions") or 0)
        commits = int(pr_data.get("commits") or 0)
        head = pr_data.get("head") if isinstance(pr_data.get("head"), dict) else {}
        base = pr_data.get("base") if isinstance(pr_data.get("base"), dict) else {}
        head_sha = str(head.get("sha") or "").strip()
        base_sha = str(base.get("sha") or "").strip()
        if head_sha or base_sha:
            sections.append(
                "Revision identity: "
                f"head_sha={head_sha or 'unknown'}, base_sha={base_sha or 'unknown'}"
            )
        sections.append(
            "PR stats: "
            f"changed_files={changed_files}, additions={additions}, deletions={deletions}, commits={commits}"
        )

        touched_paths: list[str] = []
        changed_file_entries: list[dict[str, Any]] = []
        if isinstance(files, list) and files:
            file_lines: list[str] = []
            remaining_budget = 2000
            for item in files[:8]:
                if not isinstance(item, dict):
                    continue
                filename = str(item.get("filename") or "").strip()
                if not filename:
                    continue
                touched_paths.append(filename)
                status = str(item.get("status") or "modified").strip()
                file_additions = int(item.get("additions") or 0)
                file_deletions = int(item.get("deletions") or 0)
                changes = int(item.get("changes") or 0)
                line = f"- {filename} ({status}, +{file_additions}/-{file_deletions}, changes={changes})"
                patch = str(item.get("patch") or "").strip()
                patch_excerpt = ""
                if patch:
                    clipped_patch = self._clip_text(patch, 320)
                    patch_excerpt = self._clip_text(patch, 240)
                    line += "\n  Patch excerpt:\n  " + clipped_patch.replace("\n", "\n  ")
                if len(line) > remaining_budget:
                    line = self._clip_text(line, max(120, remaining_budget))
                file_lines.append(line)
                changed_file_entries.append(
                    {
                        "filename": filename,
                        "status": status,
                        "additions": file_additions,
                        "deletions": file_deletions,
                        "changes": changes,
                        "patch_excerpt": patch_excerpt,
                    }
                )
                remaining_budget -= len(line) + 2
                if remaining_budget <= 120:
                    break
            touched_tests = [path for path in touched_paths if self._is_test_path(path)]
            risk_tags = self._derive_risk_tags(touched_paths)
            if touched_paths:
                sections.append("Touched paths:\n" + "\n".join(f"- {path}" for path in touched_paths[:8]))
            if touched_tests:
                sections.append("Touched tests:\n" + "\n".join(f"- {path}" for path in touched_tests[:8]))
            if risk_tags:
                sections.append("Risk tags: " + ", ".join(risk_tags))
            if file_lines:
                sections.append("Changed files and patch excerpts:\n" + "\n".join(file_lines))
        else:
            touched_tests = []
            risk_tags = []
        return {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "pr_stats": {
                "changed_files": changed_files,
                "additions": additions,
                "deletions": deletions,
                "commits": commits,
            },
            "touched_paths": touched_paths,
            "touched_tests": touched_tests,
            "risk_tags": risk_tags,
            "changed_files": changed_file_entries,
            "instructions_context": "\n\n".join(section for section in sections if section.strip()),
        }

    def _build_instructions(
        self,
        *,
        repo_full_name: str,
        entity_type: str,
        number: int,
        title: str,
        url: str,
        actor_login: str,
        action: str,
        imperative_verb: str,
        trigger_text: str,
        review_context: str = "",
    ) -> str:
        clipped_trigger = trigger_text.strip()
        if len(clipped_trigger) > 1200:
            clipped_trigger = clipped_trigger[:1200].rstrip() + "..."
        kind = "pull request" if entity_type == "pr" else "issue"
        instructions = (
            f"GitHub {kind} automation request.\n"
            f"Repository: {repo_full_name}\n"
            f"{kind.title()} number: {number}\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"Actor: @{actor_login}\n"
            f"Webhook action: {action}\n"
            f"Requested verb: {imperative_verb}\n"
            "Instructions:\n"
            f"{clipped_trigger}"
        )
        if entity_type == "pr":
            if review_context:
                instructions += (
                    "\n\nReview guidance:\n"
                    "Use the actual diff context below to review correctness, regressions, edge cases, "
                    "security, and missing tests. Reference concrete files or patch excerpts when possible.\n\n"
                    f"{review_context}"
                )
        if len(instructions) > 3800:
            instructions = instructions[:3800].rstrip() + "..."
        return instructions

    def _build_interbot_envelope(
        self,
        event: NormalizedGitHubEvent,
        *,
        target_node_id: Optional[str] = None,
        target_alias: Optional[str] = None,
        bridge_id: Optional[str] = None,
    ) -> dict[str, Any]:
        timestamp_ms = int(time.time() * 1000)
        status_endpoint = f"{self.config.public_base_url}/bridge/status"
        effective_target_node = target_node_id or self.config.target_node_id
        effective_target_alias = target_alias or self.config.target_alias
        effective_bridge_id = bridge_id or event.bridge_id
        status_token = self._generate_status_token(
            effective_bridge_id, effective_target_node
        )
        return {
            "spec_version": "mep.interbot.v1",
            "message_id": str(uuid.uuid4()),
            "trace_id": effective_bridge_id,
            "timestamp_ms": timestamp_ms,
            "source": {
                "node_id": self.submission_client.node_id,
                "alias": self.config.bridge_source_alias,
            },
            "target": {
                "node_id": effective_target_node,
                "alias": effective_target_alias,
            },
            "conversation": {
                "context_id": event.context_id,
                "turn_type": "chat_turn",
                "turn_index": event.event_sequence,
            },
            "intent": {
                "type": event.intent_type,
                "priority": "high",
            },
            "task": {
                "title": f"GitHub {event.entity_type.upper()} {event.repo_full_name}#{event.number}",
                "instructions": event.instructions,
                "expected_output": {"result_type": "text"},
                "inputs": {
                    "bridge_metadata": {
                        "source_type": "github",
                        "delivery_id": event.delivery_id,
                        "bridge_id": effective_bridge_id,
                        "status_endpoint": status_endpoint,
                        "status_token": status_token,
                        "event_sequence": event.event_sequence,
                        "coalesced_delivery_ids": list(event.coalesced_delivery_ids),
                    },
                    "github": {
                        **event.github_inputs,
                        "coalesced_delivery_ids": list(event.coalesced_delivery_ids),
                        "coalesced_actions": list(event.coalesced_actions),
                        "event_sequence": event.event_sequence,
                    },
                },
            },
            "economics": {
                "bounty_ns": 0,
                "currency": "MEP_NS",
                "market": "chat",
                "payment_direction": "none",
            },
            "delivery": {
                "reply_mode": "new_dm",
                "settlement_mode": "task_result",
            },
        }

    def _generate_status_token(self, bridge_id: str, target_node_id: str) -> str:
        exp = int(time.time()) + self.config.status_token_lifetime_seconds
        claims = {"bridge_id": bridge_id, "target_node_id": target_node_id, "exp": exp}
        payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(
            self.config.status_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).digest()
        return f"{_base64url_encode(payload)}.{_base64url_encode(signature)}"

    def _verify_status_token(self, token: str) -> dict[str, Any]:
        if not token or "." not in token:
            raise HTTPException(status_code=401, detail="Missing bridge status token")
        payload_part, sig_part = token.split(".", 1)
        payload = _base64url_decode(payload_part)
        provided_sig = _base64url_decode(sig_part)
        expected_sig = hmac.new(
            self.config.status_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_sig, provided_sig):
            raise HTTPException(status_code=401, detail="Invalid bridge status token")
        claims = json.loads(payload.decode("utf-8"))
        if int(claims.get("exp") or 0) < int(time.time()):
            raise HTTPException(status_code=401, detail="Expired bridge status token")
        return claims

    def _resolve_github_writeback_action(
        self,
        execution: dict[str, Any],
        update: BridgeStatusUpdate,
    ) -> str:
        explicit_action = str(update.action or "").strip().lower()
        if explicit_action:
            return explicit_action
        intent_type = str(execution.get("intent_type") or "").strip().lower()
        imperative_verb = str(execution.get("imperative_verb") or "").strip().lower()
        if intent_type == "code.review.approve" or imperative_verb == "approve":
            return "approved"
        if intent_type == "code.review.request" or imperative_verb in {"review", "check"}:
            return "reviewed"
        if intent_type == "code.review.comment" or imperative_verb == "comment":
            return "commented"
        if intent_type in {"analysis.request", "issue.triage.request"} or imperative_verb in {"analyze", "triage"}:
            return "commented"
        return "commented"

    def _render_github_writeback_body(
        self,
        bridge_id: str,
        action: str,
        detail: Optional[str],
        *,
        target_alias: Optional[str] = None,
    ) -> str:
        detail_text = (detail or "").strip() or (
            f"{target_alias or self.config.target_alias} completed the requested action."
        )
        marker = f"{BRIDGE_OUTPUT_MARKER} bridge_id={bridge_id} action={action} -->"
        return f"{detail_text}\n\n{marker}"

    def _target_alias_for_execution(self, execution: dict[str, Any]) -> str:
        return str(execution.get("target_alias") or self.config.target_alias or "").strip()

    def _lookup_alias_value(self, values: dict[str, str], target_alias: str) -> Optional[str]:
        normalized_target = _normalize_alias_key(target_alias)
        for alias, value in values.items():
            if _normalize_alias_key(alias) == normalized_target and value and value.strip():
                return value.strip()
        return None

    def _resolve_github_writeback_identity(self, execution: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        target_alias = self._target_alias_for_execution(execution)
        alias_token = self._lookup_alias_value(self.config.github_tokens_by_alias, target_alias)
        alias_login = self._lookup_alias_value(self.config.github_logins_by_alias, target_alias)
        if alias_token:
            return alias_token, alias_login or target_alias
        return self.config.github_token, self.config.github_writeback_login

    def _post_github_comment(self, repo_full_name: str, number: int, body: str, github_token: str) -> None:
        if not github_token:
            raise HTTPException(status_code=500, detail="Bridge configuration missing: GitHub writeback token")
        response = self.github_session.post(
            f"https://api.github.com/repos/{repo_full_name}/issues/{number}/comments",
            json={"body": body},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
        response.raise_for_status()

    def _submit_github_review(
        self, repo_full_name: str, number: int, event: str, body: str, github_token: str
    ) -> None:
        if not github_token:
            raise HTTPException(status_code=500, detail="Bridge configuration missing: GitHub writeback token")
        response = self.github_session.post(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{number}/reviews",
            json={"event": event, "body": body},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
        response.raise_for_status()

    def _assert_writeback_identity_allowed(self, execution: dict[str, Any]) -> None:
        target_alias = self._target_alias_for_execution(execution)
        if self._lookup_alias_value(self.config.github_tokens_by_alias, target_alias):
            return
        if not self.config.github_writeback_aliases:
            return
        normalized_target = _normalize_alias_key(target_alias)
        normalized_allowed = {
            _normalize_alias_key(alias) for alias in self.config.github_writeback_aliases if alias and alias.strip()
        }
        if normalized_target in normalized_allowed:
            return
        identity_label = self.config.github_writeback_login or "configured GitHub token"
        allowed_aliases = ", ".join(sorted(self.config.github_writeback_aliases))
        raise HTTPException(
            status_code=409,
            detail=(
                f"GitHub writeback identity {identity_label} is not allowed for target alias {target_alias!r}. "
                f"Allowed aliases: {allowed_aliases}"
            ),
        )

    def _write_back_to_github(self, execution: dict[str, Any], update: BridgeStatusUpdate) -> str:
        self._assert_writeback_identity_allowed(execution)
        github_token, _identity_label = self._resolve_github_writeback_identity(execution)
        if not github_token:
            raise HTTPException(status_code=500, detail="Bridge configuration missing: GitHub writeback token")
        repo_full_name = str(execution.get("repo_full_name") or "")
        number = int(execution.get("issue_number") or 0)
        entity_type = str(execution.get("entity_type") or "").strip().lower()
        action = self._resolve_github_writeback_action(execution, update)
        body = self._render_github_writeback_body(
            str(execution.get("bridge_id") or update.bridge_id),
            action,
            update.detail,
            target_alias=str(execution.get("target_alias") or "").strip() or None,
        )
        review_events = {
            "approved": "APPROVE",
            "reviewed": "COMMENT",
            "changes_requested": "REQUEST_CHANGES",
        }
        if entity_type == "pr" and action in review_events:
            self._submit_github_review(repo_full_name, number, review_events[action], body, github_token)
            return action
        self._post_github_comment(repo_full_name, number, body, github_token)
        return "commented"

    async def handle_status_callback(self, update: BridgeStatusUpdate, token: str) -> dict[str, Any]:
        self._require_runtime_config()
        claims = self._verify_status_token(token)
        execution = self.store.get_execution(update.bridge_id)
        if execution is None:
            raise HTTPException(status_code=404, detail="Unknown bridge_id")
        if claims.get("bridge_id") != update.bridge_id:
            raise HTTPException(status_code=403, detail="bridge_id mismatch")
        expected_target = claims.get("target_node_id")
        if expected_target and update.target_node_id and expected_target != update.target_node_id:
            raise HTTPException(status_code=403, detail="target_node_id mismatch")
        self.store.update_execution(
            update.bridge_id,
            status=update.status,
            task_id=update.task_id,
            action=update.action,
        )
        refreshed = self.store.get_execution(update.bridge_id)
        resolved_action = update.action
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Unknown bridge_id")
        if str(update.status).strip().lower() == "completed":
            resolved_action = self._write_back_to_github(refreshed, update)
            if resolved_action != update.action:
                self.store.update_execution(update.bridge_id, action=resolved_action)
                refreshed = self.store.get_execution(update.bridge_id) or refreshed
        event = NormalizedGitHubEvent(
            delivery_id="",
            source_event="",
            source_action="",
            repo_full_name=str(refreshed["repo_full_name"]),
            entity_type=str(refreshed["entity_type"]),
            number=int(refreshed["issue_number"]),
            title="",
            url="",
            actor_login="",
            author_association="",
            context_id=str(refreshed["context_id"]),
            imperative_verb=str(refreshed["imperative_verb"]),
            intent_type=str(refreshed["intent_type"]),
            instructions="",
            raw_trigger_text="",
            event_sequence=int(refreshed["event_sequence"]),
            bridge_id=update.bridge_id,
        )
        await self._notify_status(
            update.bridge_id,
            self._render_status_text(
                event,
                update.status,
                task_id=update.task_id or refreshed.get("task_id"),
                action=resolved_action,
                detail=update.detail,
                target_alias=str(refreshed.get("target_alias") or "").strip() or None,
                target_node_id=str(refreshed.get("target_node_id") or "").strip() or None,
            ),
        )
        return {"status": "ok", "bridge_id": update.bridge_id}

    def _render_status_text(
        self,
        event: NormalizedGitHubEvent,
        status: str,
        *,
        task_id: Optional[str] = None,
        action: Optional[str] = None,
        detail: Optional[str] = None,
        target_alias: Optional[str] = None,
        target_node_id: Optional[str] = None,
    ) -> str:
        kind = "PR" if event.entity_type == "pr" else "Issue"
        effective_target_alias = target_alias or self.config.target_alias
        effective_target_node_id = target_node_id or self.config.target_node_id
        lines = [
            f"{kind} {event.repo_full_name}#{event.number}",
            f"verb: {event.imperative_verb}",
            f"status: {status}",
            f"target: {effective_target_alias} ({effective_target_node_id})",
            f"bridge_id: {event.bridge_id}",
            f"context_id: {event.context_id}",
        ]
        if task_id:
            lines.append(f"task_id: {task_id}")
        if action:
            lines.append(f"action: {action}")
        if detail:
            lines.append(f"detail: {detail[:300]}")
        return "\n".join(lines)

    async def shutdown(self) -> None:
        async with self._pending_lock:
            pendings = list(self._pending_by_context.values())
            self._pending_by_context.clear()
        for pending in pendings:
            if pending.flush_task:
                pending.flush_task.cancel()


def create_app(
    *,
    config: Optional[BridgeConfig] = None,
    service: Optional[GitHubToMEPBridgeService] = None,
) -> FastAPI:
    bridge_config = config or BridgeConfig.from_env()
    bridge_service = service or GitHubToMEPBridgeService(bridge_config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await bridge_service.shutdown()

    app = FastAPI(
        title="GitHub To MEP Bridge",
        description="GitHub webhook bridge that emits actionable MEP DM tasks",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.bridge_service = bridge_service

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "bridge": "github_to_mep",
            "multi_target": bool(bridge_service.config.alias_map),
            "alias_map": bridge_service.config.alias_map,
            "target_node_id": bridge_service.config.target_node_id,
            "coalesce_window_seconds": bridge_service.config.coalesce_window_seconds,
        }

    @app.post("/github/webhook")
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(...),
        x_github_delivery: str = Header(...),
        x_hub_signature_256: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        body = await request.body()
        bridge_service.verify_github_signature(body, x_hub_signature_256)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="GitHub webhook payload must be an object")
        return await bridge_service.handle_github_webhook(
            delivery_id=x_github_delivery,
            github_event=x_github_event,
            payload=payload,
        )

    @app.post("/bridge/status")
    async def bridge_status(
        update: BridgeStatusUpdate,
        authorization: Optional[str] = Header(default=None),
        x_bridge_status_token: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        token = x_bridge_status_token
        if not token and authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
        return await bridge_service.handle_status_callback(update, token or "")

    return app


app = create_app()
