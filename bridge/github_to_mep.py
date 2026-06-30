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
_WEAK_GITHUB_REVIEW_PATTERNS = [
    r"\blooks good\b",
    r"\blooks correct\b",
    r"\bwell-scoped\b",
    r"\bwell scoped\b",
    r"\bwell-contained\b",
    r"\bwell contained\b",
    r"\bminimal and well-scoped\b",
    r"\bchanges look correct\b",
    r"\btests pass\b",
    r"\bno regressions\b",
    r"\bno blocking issues\b",
    r"\bfocused runtime tests\b",
]
_SPECULATIVE_FINDING_PATTERNS = [
    r"\bif intended\b",
    r"\bif .*? intended for\b",
    r"\bif .*? meant to\b",
    r"\bsuggests incomplete implementation\b",
    r"\bpotentially leaving\b",
    r"\bappears to\b",
    r"\bseems to\b",
    r"\bcould indicate\b",
]


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

    @staticmethod
    def _decode_json_dict(raw: Any) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

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
                instructions TEXT NOT NULL DEFAULT '',
                github_inputs_json TEXT NOT NULL DEFAULT '{}',
                review_result_json TEXT NOT NULL DEFAULT '{}',
                retry_count INTEGER NOT NULL DEFAULT 0,
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
        if "retry_count" not in execution_columns:
            cursor.execute(
                """
                ALTER TABLE bridge_executions
                ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0
                """
            )
        if "instructions" not in execution_columns:
            cursor.execute(
                """
                ALTER TABLE bridge_executions
                ADD COLUMN instructions TEXT NOT NULL DEFAULT ''
                """
            )
        if "github_inputs_json" not in execution_columns:
            cursor.execute(
                """
                ALTER TABLE bridge_executions
                ADD COLUMN github_inputs_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        if "review_result_json" not in execution_columns:
            cursor.execute(
                """
                ALTER TABLE bridge_executions
                ADD COLUMN review_result_json TEXT NOT NULL DEFAULT '{}'
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
        instructions: str = "",
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
                status, instructions, github_inputs_json, review_result_json, retry_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 0, ?, ?)
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
                instructions,
                json.dumps(event.github_inputs or {}, separators=(",", ":"), sort_keys=True),
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
        review_result: Optional[dict[str, Any]] = None,
        retry_count: Optional[int] = None,
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
        if review_result is not None:
            assignments.append("review_result_json = ?")
            params.append(json.dumps(review_result, separators=(",", ":"), sort_keys=True))
        if retry_count is not None:
            assignments.append("retry_count = ?")
            params.append(retry_count)
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
        if row is None:
            return None
        record = dict(row)
        record["github_inputs"] = self._decode_json_dict(record.get("github_inputs_json"))
        record["review_result"] = self._decode_json_dict(record.get("review_result_json"))
        return record

    def list_recent_review_trials(self, *, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT *
            FROM bridge_executions
            WHERE entity_type = 'pr' AND review_result_json != '{}'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        )
        rows = cursor.fetchall()
        conn.close()
        items: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            items.append(
                {
                    "bridge_id": str(record.get("bridge_id") or ""),
                    "context_id": str(record.get("context_id") or ""),
                    "repo_full_name": str(record.get("repo_full_name") or ""),
                    "issue_number": int(record.get("issue_number") or 0),
                    "target_alias": str(record.get("target_alias") or ""),
                    "intent_type": str(record.get("intent_type") or ""),
                    "status": str(record.get("status") or ""),
                    "action": str(record.get("action") or ""),
                    "retry_count": int(record.get("retry_count") or 0),
                    "updated_at": float(record.get("updated_at") or 0),
                    "review_result": self._decode_json_dict(record.get("review_result_json")),
                }
            )
        return items


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
    MAX_RETRIES = 2

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
        self.github_writeback_metrics: dict[str, Any] = {
            "attempts": 0,
            "reviews_published": 0,
            "comments_published": 0,
            "suppressed_weak_reviews": 0,
            "suppressed_approvals": 0,
            "last_action": None,
            "last_detail_preview": None,
            "last_suppressed_reason": None,
            "last_suppressed_at": None,
            "last_quality_score": 0,
            "last_quality_reasons": [],
        }

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
        if incoming.github_inputs:
            existing.github_inputs = {
                **existing.github_inputs,
                **incoming.github_inputs,
            }
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
                instructions=pending.event.instructions or "",
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
            compact_review_package = self._compact_review_package_for_task_inputs(review_package)
            for key, value in compact_review_package.items():
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

    @staticmethod
    def _extract_trigger_verb(text: str) -> Optional[str]:
        command_match = re.match(
            r"""
            (?:\s+|[,:]\s*)*
            (?:(?:please|pls|kindly)\s+)*
            (?P<verb>re(?:\s+|-)?review|rereview|[a-z][a-z_-]*)\b
            """,
            text,
            re.IGNORECASE | re.VERBOSE,
        )
        if not command_match:
            return None
        verb = re.sub(r"[\s-]+", "", command_match.group("verb").strip().lower())
        if verb == "rereview":
            return "review"
        return verb

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
            verb = self._extract_trigger_verb(text[cursor:])
            if not verb:
                continue
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

    def _compact_review_package_for_task_inputs(self, review_package: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(review_package, dict):
            return {}
        compact: dict[str, Any] = {}
        for key in (
            "head_sha",
            "base_sha",
            "head_ref",
            "base_ref",
            "repo_clone_url",
            "pr_stats",
            "ci_checks",
            "touched_paths",
            "touched_tests",
            "risk_tags",
            "reviewability",
        ):
            value = review_package.get(key)
            if value not in (None, "", [], {}):
                compact[key] = value

        instructions_context = str(review_package.get("instructions_context") or "").strip()
        if instructions_context:
            compact["instructions_context"] = self._clip_text(instructions_context, 2200)

        changed_files = review_package.get("changed_files")
        compact_files: list[dict[str, Any]] = []
        if isinstance(changed_files, list):
            remaining_patch_budget = 1200
            for item in changed_files[:6]:
                if not isinstance(item, dict):
                    continue
                filename = str(item.get("filename") or "").strip()
                if not filename:
                    continue
                entry = {
                    "filename": filename,
                    "status": str(item.get("status") or "modified").strip(),
                    "additions": int(item.get("additions") or 0),
                    "deletions": int(item.get("deletions") or 0),
                    "changes": int(item.get("changes") or 0),
                }
                patch_excerpt = str(item.get("patch_excerpt") or "").strip()
                if patch_excerpt and remaining_patch_budget > 0:
                    clipped_excerpt = self._clip_text(patch_excerpt, min(remaining_patch_budget, 240))
                    entry["patch_excerpt"] = clipped_excerpt
                    remaining_patch_budget -= len(clipped_excerpt)
                compact_files.append(entry)
        if compact_files:
            compact["changed_files"] = compact_files
        return compact

    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = path.strip().lower()
        if not lowered:
            return False
        basename = lowered.rsplit("/", 1)[-1]
        return (
            lowered.startswith("tests/")
            or lowered.startswith("test/")
            or "/tests/" in lowered
            or "/test/" in lowered
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
            ("security", ("security", "sanitize", "escape", "validate", "xss", "csrf", "injection")),
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

    @staticmethod
    def _is_docs_only_path(path: str) -> bool:
        lowered = str(path or "").strip().lower()
        if not lowered:
            return False
        if lowered.startswith("docs/"):
            return True
        basename = lowered.rsplit("/", 1)[-1]
        if basename in {
            "readme",
            "readme.md",
            "contributing.md",
            "license",
            "license.md",
            "changelog.md",
            "security.md",
        }:
            return True
        return any(lowered.endswith(suffix) for suffix in (".md", ".rst", ".txt", ".adoc"))

    @classmethod
    def _classify_reviewability(
        cls,
        *,
        touched_paths: list[str],
        touched_tests: list[str],
        risk_tags: list[str],
        pr_stats: dict[str, Any],
    ) -> dict[str, Any]:
        changed_files = int(pr_stats.get("changed_files") or 0)
        additions = int(pr_stats.get("additions") or 0)
        deletions = int(pr_stats.get("deletions") or 0)
        normalized_paths = [str(path).strip() for path in touched_paths if str(path).strip()]
        if normalized_paths and all(cls._is_docs_only_path(path) for path in normalized_paths):
            return {
                "bucket": "low_signal",
                "reasons": ["docs_only_patch"],
                "publish_no_finding": False,
                "summary": "Docs-only patch with low behavioral signal; suppress generic no-finding review writeback unless a concrete issue is found.",
            }
        if (
            normalized_paths
            and not risk_tags
            and not touched_tests
            and changed_files <= 1
            and additions + deletions <= 12
            and all(cls._is_test_path(path) for path in normalized_paths)
        ):
            return {
                "bucket": "low_signal",
                "reasons": ["single_small_test_only_patch"],
                "publish_no_finding": False,
                "summary": "Small test-only patch; publish only if the reviewer finds a concrete regression or unsupported expectation.",
            }
        return {
            "bucket": "standard",
            "reasons": [],
            "publish_no_finding": True,
            "summary": "Standard review path; publish grounded findings or sufficiently concrete no-finding reviews.",
        }

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
        head_ref = str(head.get("ref") or "").strip()
        base_ref = str(base.get("ref") or "").strip()
        repo_clone_url = str((head.get("repo") or {}).get("clone_url") or "").strip()
        check_runs_payload: Any = {}
        if head_sha:
            try:
                check_runs_response = self.github_session.get(
                    f"https://api.github.com/repos/{repo_full_name}/commits/{head_sha}/check-runs?per_page=20",
                    headers=headers,
                    timeout=20,
                )
                if check_runs_response.status_code == 200:
                    check_runs_payload = check_runs_response.json()
            except Exception:
                check_runs_payload = {}
        ci_checks = self._summarize_pr_checks(check_runs_payload)

        if head_sha or base_sha:
            sections.append(
                "Revision identity: "
                f"head_sha={head_sha or 'unknown'}, base_sha={base_sha or 'unknown'}, "
                f"head_ref={head_ref or 'unknown'}, base_ref={base_ref or 'unknown'}"
            )
        if ci_checks.get("has_checks"):
            check_lines = [
                f"- {item.get('name')}: {item.get('state')}"
                for item in ci_checks.get("summary", [])
                if isinstance(item, dict)
            ]
            sections.append(
                "PR checks: "
                f"state={ci_checks.get('state')}\n" + "\n".join(check_lines)
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
                        "patch": patch,
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
            reviewability = self._classify_reviewability(
                touched_paths=touched_paths,
                touched_tests=touched_tests,
                risk_tags=risk_tags,
                pr_stats={
                    "changed_files": changed_files,
                    "additions": additions,
                    "deletions": deletions,
                    "commits": commits,
                },
            )
            sections.append(
                "Reviewability assessment: "
                f"bucket={reviewability.get('bucket', 'standard')}; "
                f"reasons={', '.join(reviewability.get('reasons') or ['none'])}; "
                f"guidance={reviewability.get('summary', '')}"
            )
            if file_lines:
                sections.append("Changed files and patch excerpts:\n" + "\n".join(file_lines))
        else:
            touched_tests = []
            risk_tags = []
            reviewability = {
                "bucket": "standard",
                "reasons": [],
                "publish_no_finding": True,
                "summary": "Standard review path; publish grounded findings or sufficiently concrete no-finding reviews.",
            }
        return {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "head_ref": head_ref,
            "base_ref": base_ref,
            "repo_clone_url": repo_clone_url,
            "pr_stats": {
                "changed_files": changed_files,
                "additions": additions,
                "deletions": deletions,
                "commits": commits,
            },
            "ci_checks": ci_checks,
            "touched_paths": touched_paths,
            "touched_tests": touched_tests,
            "risk_tags": risk_tags,
            "reviewability": reviewability,
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
                    "Primary objective: identify the highest-value correctness, regression, edge-case, security, "
                    "or missing-validation risk in the actual changed code before summarizing anything. "
                    "If you do not find a concrete issue, explicitly state which risk areas you checked, which checks "
                    "you performed against the diff/workspace/tests, and why no finding is warranted. "
                    "Reference concrete files, changed identifiers, or patch excerpts when possible.\n\n"
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
                        **(event.github_inputs or {}),
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

    @staticmethod
    def _detail_preview(detail: Optional[str], *, max_chars: int = 240) -> Optional[str]:
        text = re.sub(r"\s+", " ", str(detail or "").strip())
        if not text:
            return None
        return text[:max_chars]

    @staticmethod
    def _normalize_review_reference(value: str) -> str:
        text = str(value or "").strip().strip("`'\"")
        text = text.lstrip("([{")
        text = text.rstrip(".,;:)]}")
        return text.replace("\\", "/").strip().lower()

    @classmethod
    def _extract_path_like_references(cls, detail: str) -> set[str]:
        if not detail:
            return set()
        refs: set[str] = set()
        patterns = (
            r"`([^`\n]+)`",
            r"(?<![\w/.-])(\.[A-Za-z0-9_.-]+)(?![\w/.-])",
            r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]*[A-Za-z0-9_.-]/?)(?![\w/.-])",
            r"(?<![\w/.-])([A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|php|json|yml|yaml|md|txt|cfg|ini|toml|log))(?![\w/.-])",
        )
        for pattern in patterns:
            for match in re.findall(pattern, detail, re.IGNORECASE):
                normalized = cls._normalize_review_reference(match)
                if normalized and ("/" in normalized or "." in normalized):
                    refs.add(normalized)
        return refs

    @classmethod
    def _match_path_reference(cls, reference: str, expected_paths: list[str]) -> set[str]:
        normalized_ref = cls._normalize_review_reference(reference)
        if not normalized_ref:
            return set()
        matched: set[str] = set()
        for path in expected_paths:
            normalized_path = cls._normalize_review_reference(path)
            if not normalized_path:
                continue
            basename = normalized_path.rsplit("/", 1)[-1]
            if normalized_ref in {normalized_path, basename}:
                matched.add(path)
                continue
            if normalized_ref.endswith("/") and normalized_path.startswith(normalized_ref):
                matched.add(path)
        return matched

    @classmethod
    def _grounded_review_paths(cls, detail: str, expected_paths: list[str]) -> set[str]:
        refs = cls._extract_path_like_references(detail)
        if not expected_paths:
            return refs
        matched: set[str] = set()
        for ref in refs:
            matched.update(cls._match_path_reference(ref, expected_paths))
        return matched

    @staticmethod
    def _extract_review_finding_entries(detail: str) -> list[tuple[str, str]]:
        if not detail:
            return []
        findings: list[tuple[str, str]] = []
        pattern = re.compile(
            r"^\d+\.\s+\*\*(?P<issue>.+?)\*\*(?:\s+\(`(?P<file>[^`]+)`\))?:\s*(?P<rationale>.+)$",
            re.MULTILINE,
        )
        for match in pattern.finditer(detail):
            file_hint = str(match.group("file") or "").strip()
            issue = str(match.group("issue") or "").strip()
            rationale = str(match.group("rationale") or "").strip()
            combined = " ".join(part for part in (issue, rationale) if part).strip()
            if combined:
                findings.append((file_hint, combined))
        return findings

    @staticmethod
    def _extract_identifier_tokens(text: str) -> set[str]:
        if not text:
            return set()
        excluded = {
            "node_id",
            "task_id",
            "repo_name",
            "issue_number",
            "bridge_id",
            "target_node_id",
        }
        tokens: set[str] = set()
        for token in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text):
            lowered = token.lower()
            if len(lowered) >= 4 and lowered not in excluded:
                tokens.add(lowered)
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b", text):
            lowered = token.lower()
            if len(lowered) >= 4 and lowered not in excluded:
                tokens.add(lowered)
        return tokens

    @staticmethod
    def _extract_observation_text(detail: str) -> str:
        if not detail:
            return ""
        match = re.search(r"Observation:\s*(.+)", detail, re.IGNORECASE)
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    @staticmethod
    def _extract_review_section_text(detail: str, label: str) -> str:
        if not detail:
            return ""
        match = re.search(rf"{re.escape(label)}:\s*(.+)", detail, re.IGNORECASE)
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    @classmethod
    def _extract_review_section_list(cls, detail: str, label: str) -> list[str]:
        text = cls._extract_review_section_text(detail, label)
        if not text:
            return []
        values: list[str] = []
        for part in text.split(","):
            cleaned = re.sub(r"\s+", " ", str(part or "").strip(" `")).strip()
            if cleaned and cleaned not in values:
                values.append(cleaned)
        return values

    @classmethod
    def _grounded_code_tokens(cls, text: str, patch_info: dict[str, str]) -> set[str]:
        if not text or not patch_info:
            return set()
        tokens = cls._extract_identifier_tokens(text)
        full_text = patch_info.get("full", "").lower()
        changed_text = patch_info.get("changes", "").lower()

        # Priority: tokens in actual changes
        grounded = {token for token in tokens if token in changed_text}
        # Secondary: tokens in context lines
        context_grounded = {token for token in tokens if token in full_text}

        return grounded | context_grounded

    @staticmethod
    def _is_speculative_finding(text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        return any(re.search(pattern, lowered) for pattern in _SPECULATIVE_FINDING_PATTERNS)

    @staticmethod
    def _is_auth_absence_claim(text: str) -> bool:
        lowered = str(text or "").lower()
        if "authentication" not in lowered and "authorization" not in lowered:
            return False
        return any(token in lowered for token in (" has no ", " no ", " without ", " missing "))

    @staticmethod
    def _finding_conflicts_with_patch(finding_text: str, patch_text: str) -> bool:
        if not finding_text or not patch_text:
            return False
        lowered_finding = finding_text.lower()
        lowered_patch = patch_text.lower()
        auth_absence_patterns = (
            r"\bno authentication\b",
            r"\bno authorization\b",
            r"\bhas no authentication\b",
            r"\bhas no authorization\b",
            r"\bwithout authentication\b",
            r"\bwithout authorization\b",
            r"\bwithout any authentication\b",
            r"\bwithout any authorization\b",
            r"\bmissing authentication\b",
            r"\bmissing authorization\b",
        )
        auth_evidence_tokens = (
            "verify_request",
            "authenticated_",
            "authorization",
            "permission",
            "forbidden",
            "403",
            "depends(",
        )
        if any(re.search(pattern, lowered_finding) for pattern in auth_absence_patterns):
            return any(token in lowered_patch for token in auth_evidence_tokens)
        if GitHubToMEPBridgeService._is_auth_absence_claim(lowered_finding):
            return any(token in lowered_patch for token in auth_evidence_tokens)
        return False

    @staticmethod
    def _patch_text_by_path(review_package: dict[str, Any]) -> dict[str, dict[str, str]]:
        patches: dict[str, dict[str, str]] = {}
        changed_files = review_package.get("changed_files")
        if not isinstance(changed_files, list):
            return patches
        for item in changed_files:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "").strip()
            patch = str(item.get("patch") or "").strip()
            if filename and patch:
                lowered_patch = patch.lower()
                changes_only = "\n".join(
                    line[1:] for line in lowered_patch.splitlines() if line.startswith(("+", "-"))
                )
                patches[filename] = {
                    "full": lowered_patch,
                    "changes": changes_only,
                }
        return patches

    @staticmethod
    def _summarize_pr_checks(check_runs_payload: Any) -> dict[str, Any]:
        raw_runs = check_runs_payload.get("check_runs") if isinstance(check_runs_payload, dict) else []
        if not isinstance(raw_runs, list):
            raw_runs = []
        summary: list[dict[str, str]] = []
        pending_count = 0
        failing_count = 0
        successful_count = 0
        for item in raw_runs[:8]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "unnamed-check").strip() or "unnamed-check"
            status = str(item.get("status") or "").strip().lower()
            conclusion = str(item.get("conclusion") or "").strip().lower()
            if status and status != "completed":
                state = status
                pending_count += 1
            elif conclusion in {"success", "neutral", "skipped"}:
                state = conclusion or "success"
                successful_count += 1
            else:
                state = conclusion or "unknown"
                failing_count += 1
            summary.append(
                {
                    "name": name,
                    "status": status,
                    "conclusion": conclusion,
                    "state": state,
                }
            )
        if summary:
            if pending_count:
                overall_state = "pending"
            elif failing_count:
                overall_state = "failing"
            elif successful_count == len(summary):
                overall_state = "green"
            else:
                overall_state = "unknown"
        else:
            overall_state = "none"
        return {
            "has_checks": bool(summary),
            "state": overall_state,
            "all_green": bool(summary) and pending_count == 0 and failing_count == 0,
            "pending_count": pending_count,
            "failing_count": failing_count,
            "summary": summary,
        }

    def _build_review_snapshot(self, execution: dict[str, Any], detail: Optional[str]) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", str(detail or "").strip())
        lowered = normalized.lower()
        review_package: dict[str, Any] = {}
        entity_type = str(execution.get("entity_type") or "").strip().lower()
        repo_full_name = str(execution.get("repo_full_name") or "").strip()
        number = int(execution.get("issue_number") or 0)
        if entity_type == "pr" and repo_full_name and number > 0:
            review_package = self._fetch_pr_review_package(repo_full_name, number)
        expected_paths: list[str] = []
        expected_tests: list[str] = []
        for key in ("touched_paths", "touched_tests"):
            values = review_package.get(key)
            if isinstance(values, list):
                for value in values:
                    text = str(value or "").strip()
                    if not text:
                        continue
                    if text not in expected_paths:
                        expected_paths.append(text)
                    if key == "touched_tests" and text not in expected_tests:
                        expected_tests.append(text)
        anchored_paths = self._grounded_review_paths(detail or "", expected_paths)
        anchored_tests = self._grounded_review_paths(detail or "", expected_tests)
        patches_by_path = self._patch_text_by_path(review_package)
        anchored_patch_info = {
            "full": "\n".join(patches_by_path.get(path, {}).get("full", "") for path in anchored_paths),
            "changes": "\n".join(patches_by_path.get(path, {}).get("changes", "") for path in anchored_paths),
        }
        has_findings = "## review findings" in lowered or bool(self._extract_review_finding_entries(detail or ""))
        observation_text = self._extract_observation_text(detail or "")
        risk_areas_checked = self._extract_review_section_list(detail or "", "Risk areas checked")
        checks_performed = self._extract_review_section_list(detail or "", "Checks performed")
        why_no_finding = self._extract_review_section_text(detail or "", "Why no finding")
        has_structured_sections = any(
            snippet in lowered
            for snippet in (
                "## review summary",
                "## review findings",
                "observation:",
                "touched paths reviewed:",
                "tests reviewed:",
                "risk areas checked:",
                "checks performed:",
                "why no finding:",
            )
        )
        grounded_tokens = self._grounded_code_tokens(detail or "", anchored_patch_info)
        changed_tokens = {token for token in grounded_tokens if token in anchored_patch_info["changes"]}
        mentions_tests = bool(anchored_tests)
        if not mentions_tests and expected_tests:
            mentions_tests = bool(re.search(r"\btest(?:s|ed|ing)?\b", lowered))
        return {
            "normalized": normalized,
            "lowered": lowered,
            "review_package": review_package,
            "ci_checks": review_package.get("ci_checks") or {},
            "reviewability": review_package.get("reviewability") or {},
            "expected_paths": expected_paths,
            "expected_tests": expected_tests,
            "anchored_paths": anchored_paths,
            "anchored_tests": anchored_tests,
            "patches_by_path": patches_by_path,
            "anchored_patch_info": anchored_patch_info,
            "has_findings": has_findings,
            "observation_text": observation_text,
            "risk_areas_checked": risk_areas_checked,
            "checks_performed": checks_performed,
            "why_no_finding": why_no_finding,
            "has_structured_sections": has_structured_sections,
            "grounded_tokens": grounded_tokens,
            "changed_tokens": changed_tokens,
            "mentions_tests": mentions_tests,
        }

    def _score_review_quality(self, snapshot: dict[str, Any], *, action: str) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        anchored_paths = snapshot.get("anchored_paths") or set()
        changed_tokens = snapshot.get("changed_tokens") or set()
        grounded_tokens = snapshot.get("grounded_tokens") or set()
        has_findings = bool(snapshot.get("has_findings"))
        observation_text = str(snapshot.get("observation_text") or "").strip()
        risk_areas_checked = snapshot.get("risk_areas_checked") or []
        checks_performed = snapshot.get("checks_performed") or []
        why_no_finding = str(snapshot.get("why_no_finding") or "").strip()
        expected_tests = snapshot.get("expected_tests") or []
        mentions_tests = bool(snapshot.get("mentions_tests"))
        lowered = str(snapshot.get("lowered") or "")

        if anchored_paths:
            score += 1
            reasons.append("touched_path_anchor")
        if len(anchored_paths) >= 2:
            score += 1
            reasons.append("multi_path_anchor")
        if changed_tokens:
            score += 1
            reasons.append("changed_code_evidence")
        if len(changed_tokens) >= 2:
            score += 1
            reasons.append("multiple_changed_identifiers")
        if has_findings:
            score += 2
            reasons.append("concrete_findings")
        elif observation_text and grounded_tokens:
            score += 1
            reasons.append("grounded_observation")
        if risk_areas_checked:
            score += 1
            reasons.append("risk_coverage")
        if checks_performed:
            score += 1
            reasons.append("explicit_checks")
        if why_no_finding and not has_findings:
            score += 1
            reasons.append("why_no_finding")
        if mentions_tests:
            score += 1
            reasons.append("test_awareness")
        elif not expected_tests and re.search(r"\btest(?:s|ed|ing)?\b", lowered):
            score += 1
            reasons.append("general_test_awareness")
        if action == "approved" and "no risky changes" in lowered:
            score += 1
            reasons.append("explicit_low_risk_claim")
        return score, reasons

    def _approval_quality_failure(self, snapshot: dict[str, Any], score: int) -> Optional[str]:
        ci_checks = snapshot.get("ci_checks") or {}
        if ci_checks.get("has_checks"):
            if ci_checks.get("state") == "pending":
                return "approval_checks_pending"
            if not ci_checks.get("all_green"):
                return "approval_checks_not_green"
        if snapshot.get("has_findings"):
            return "approval_contains_findings"
        if not snapshot.get("anchored_paths"):
            return "approval_without_touched_path_anchor"
        if not snapshot.get("changed_tokens"):
            return "approval_without_changed_code_evidence"
        if snapshot.get("expected_tests") and not snapshot.get("mentions_tests"):
            return "approval_without_test_awareness"
        if score < 4:
            return "approval_below_quality_bar"
        return None

    def _record_review_quality(self, score: int, reasons: list[str]) -> None:
        metrics = self.github_writeback_metrics
        metrics["last_quality_score"] = int(score)
        metrics["last_quality_reasons"] = list(reasons)

    @staticmethod
    def _suppression_reason_allows_retry(reason: Optional[str]) -> bool:
        return reason not in {"approval_checks_pending", "approval_checks_not_green", "low_signal_no_finding"}

    @classmethod
    def _verify_review_findings_against_patch(
        cls,
        detail: str,
        review_package: dict[str, Any],
        expected_paths: list[str],
    ) -> Optional[str]:
        findings = cls._extract_review_finding_entries(detail)
        if not findings or not review_package or not expected_paths:
            return None
        patches_by_path = cls._patch_text_by_path(review_package)
        for file_hint, finding_text in findings:
            if cls._is_auth_absence_claim(finding_text) and not cls._extract_identifier_tokens(finding_text):
                return "ungrounded_finding"
            matched_paths = cls._match_path_reference(file_hint, expected_paths) if file_hint else set()
            if not matched_paths:
                matched_paths = cls._grounded_review_paths(finding_text, expected_paths)
            if not matched_paths:
                return "finding_without_touched_path"
            patch_info = {
                "full": "\n".join(patches_by_path.get(path, {}).get("full", "") for path in matched_paths),
                "changes": "\n".join(patches_by_path.get(path, {}).get("changes", "") for path in matched_paths),
            }
            if not patch_info["full"]:
                continue
            if cls._finding_conflicts_with_patch(finding_text, patch_info["full"]):
                return "ungrounded_finding"
            identifier_tokens = cls._extract_identifier_tokens(finding_text)
            if identifier_tokens and not any(token in patch_info["full"] for token in identifier_tokens):
                return "ungrounded_finding"
            
            grounded_tokens = cls._grounded_code_tokens(finding_text, patch_info)
            changed_tokens = {t for t in grounded_tokens if t in patch_info["changes"]}
            
            if identifier_tokens and not changed_tokens:
                # Finding mentions code identifiers, but none of them are in the actual diff (+/-)
                return "finding_in_context_only"

            if cls._is_speculative_finding(finding_text) and len(grounded_tokens) < 2:
                return "speculative_finding"
        return None

    def _classify_review_writeback_detail(
        self,
        execution: dict[str, Any],
        detail: Optional[str],
        *,
        snapshot: Optional[dict[str, Any]] = None,
        action: Optional[str] = None,
    ) -> tuple[bool, str]:
        snapshot = snapshot or self._build_review_snapshot(execution, detail)
        normalized = str(snapshot.get("normalized") or "")
        if not normalized:
            return True, "empty_detail"
        lowered = str(snapshot.get("lowered") or "")
        review_package = snapshot.get("review_package") or {}
        expected_paths = list(snapshot.get("expected_paths") or [])
        anchored_paths = snapshot.get("anchored_paths") or set()
        anchored_patch_info = snapshot.get("anchored_patch_info") or {"full": "", "changes": ""}
        has_findings = bool(snapshot.get("has_findings"))
        observation_text = str(snapshot.get("observation_text") or "")
        risk_areas_checked = snapshot.get("risk_areas_checked") or []
        checks_performed = snapshot.get("checks_performed") or []
        has_structured_sections = bool(snapshot.get("has_structured_sections"))
        why_no_finding = str(snapshot.get("why_no_finding") or "").strip()
        grounded_tokens = snapshot.get("grounded_tokens") or set()
        changed_tokens = snapshot.get("changed_tokens") or set()
        reviewability = snapshot.get("reviewability") or {}
        reviewability_bucket = str(reviewability.get("bucket") or "standard").strip().lower()
        if has_findings and not anchored_paths:
            return True, "finding_without_touched_path"
        finding_reason = self._verify_review_findings_against_patch(detail or "", review_package, expected_paths)
        if finding_reason:
            return True, finding_reason

        if has_findings and self._is_speculative_finding(detail or "") and len(grounded_tokens) < 2:
            return True, "speculative_finding"
        if has_findings and anchored_paths and review_package:
            if self._finding_conflicts_with_patch(detail or "", anchored_patch_info["full"]):
                return True, "ungrounded_finding"
        if reviewability_bucket == "low_signal" and not has_findings and action != "approved":
            return True, "low_signal_no_finding"
        if has_structured_sections and not has_findings:
            if not observation_text and not grounded_tokens and not checks_performed and not risk_areas_checked:
                return True, "summary_without_code_evidence"
            observation_tokens = self._extract_identifier_tokens(observation_text)
            
            # Phase 3A: Summary-only reviews must anchor to at least one changed token if they mention code
            if observation_tokens and not changed_tokens:
                return True, "observation_in_context_only"

            if observation_text and not grounded_tokens and len(observation_tokens) < 2:
                return True, "generic_observation"
            why_tokens = self._extract_identifier_tokens(why_no_finding)
            if why_tokens and not changed_tokens:
                return True, "summary_without_changed_behavior_evidence"
        if len(normalized) < 90 and not anchored_paths:
            return True, "too_short"
        if any(re.search(pattern, lowered) for pattern in _WEAK_GITHUB_REVIEW_PATTERNS) and not anchored_paths:
            return True, "generic_summary"
        if has_structured_sections and not anchored_paths:
            return True, "no_touched_path_anchor"
        if has_structured_sections and not has_findings and action != "approved":
            if not risk_areas_checked and not checks_performed:
                return True, "summary_without_risk_coverage"
        return False, "concrete"

    def _record_github_writeback_attempt(self, action: str, detail: Optional[str]) -> None:
        metrics = self.github_writeback_metrics
        metrics["attempts"] += 1
        metrics["last_action"] = action
        metrics["last_detail_preview"] = self._detail_preview(detail)

    def _record_github_writeback_publish(self, action: str, *, review_action: bool) -> None:
        metrics = self.github_writeback_metrics
        if review_action:
            metrics["reviews_published"] += 1
        else:
            metrics["comments_published"] += 1
        metrics["last_action"] = action
        metrics["last_suppressed_reason"] = None

    def _record_github_writeback_suppression(
        self,
        *,
        bridge_id: str,
        action: str,
        detail: Optional[str],
        reason: str,
    ) -> None:
        metrics = self.github_writeback_metrics
        metrics["suppressed_weak_reviews"] += 1
        if action == "approved":
            metrics["suppressed_approvals"] += 1
        metrics["last_action"] = "suppressed"
        metrics["last_suppressed_reason"] = reason
        metrics["last_suppressed_at"] = time.time()
        metrics["last_detail_preview"] = self._detail_preview(detail)
        print(
            "[bridge] suppressed weak GitHub review writeback "
            f"bridge_id={bridge_id} action={action} reason={reason} detail={metrics['last_detail_preview'] or '<empty>'}"
        )

    @staticmethod
    def _sorted_sample(values: set[str], *, limit: int) -> list[str]:
        return sorted(str(value) for value in values if str(value).strip())[:limit]

    def _build_review_trial_result(
        self,
        execution: dict[str, Any],
        update: BridgeStatusUpdate,
        *,
        attempted_action: str,
        resolved_action: str,
        review_action: bool,
        snapshot: dict[str, Any],
        score: int,
        reasons: list[str],
        suppression_reason: Optional[str],
    ) -> dict[str, Any]:
        review_package = snapshot.get("review_package") or {}
        ci_checks = snapshot.get("ci_checks") or {}
        anchored_paths = snapshot.get("anchored_paths") or set()
        changed_tokens = snapshot.get("changed_tokens") or set()
        grounded_tokens = snapshot.get("grounded_tokens") or set()
        expected_tests = list(snapshot.get("expected_tests") or [])
        reviewability = snapshot.get("reviewability") or {}
        ci_summary = []
        for item in ci_checks.get("summary", []):
            if isinstance(item, dict):
                ci_summary.append(
                    {
                        "name": str(item.get("name") or "").strip(),
                        "state": str(item.get("state") or "").strip(),
                    }
                )
        return {
            "recorded_at": time.time(),
            "status": str(update.status or "").strip().lower(),
            "attempted_action": attempted_action,
            "resolved_action": resolved_action,
            "review_action": bool(review_action),
            "published": bool(review_action and resolved_action in {"approved", "reviewed", "changes_requested"}),
            "suppressed": bool(suppression_reason),
            "suppression_reason": suppression_reason,
            "quality_score": int(score),
            "quality_reasons": list(reasons),
            "repo_full_name": str(execution.get("repo_full_name") or ""),
            "issue_number": int(execution.get("issue_number") or 0),
            "target_alias": str(execution.get("target_alias") or ""),
            "intent_type": str(execution.get("intent_type") or ""),
            "head_sha": str(review_package.get("head_sha") or ""),
            "ci_state": str(ci_checks.get("state") or "none"),
            "ci_has_checks": bool(ci_checks.get("has_checks")),
            "ci_all_green": bool(ci_checks.get("all_green")),
            "ci_summary": ci_summary,
            "anchored_paths": self._sorted_sample(anchored_paths, limit=6),
            "anchored_path_count": len(anchored_paths),
            "changed_token_sample": self._sorted_sample(changed_tokens, limit=8),
            "changed_token_count": len(changed_tokens),
            "grounded_token_count": len(grounded_tokens),
            "expected_tests": [str(item) for item in expected_tests[:6]],
            "expected_test_count": len(expected_tests),
            "reviewability_bucket": str(reviewability.get("bucket") or "standard"),
            "reviewability_reasons": [str(item) for item in (reviewability.get("reasons") or [])[:4]],
            "mentions_tests": bool(snapshot.get("mentions_tests")),
            "has_findings": bool(snapshot.get("has_findings")),
            "detail_preview": self._detail_preview(update.detail),
            "retry_queued": False,
            "retry_count": int(execution.get("retry_count") or 0),
        }

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

    @staticmethod
    def _execution_github_inputs(execution: dict[str, Any]) -> dict[str, Any]:
        raw = str(execution.get("github_inputs_json") or "").strip()
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

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

    def _write_back_to_github(
        self, execution: dict[str, Any], update: BridgeStatusUpdate
    ) -> tuple[str, Optional[str], Optional[dict[str, Any]]]:
        self._assert_writeback_identity_allowed(execution)
        github_token, _identity_label = self._resolve_github_writeback_identity(execution)
        if not github_token:
            raise HTTPException(status_code=500, detail="Bridge configuration missing: GitHub writeback token")
        repo_full_name = str(execution.get("repo_full_name") or "")
        number = int(execution.get("issue_number") or 0)
        entity_type = str(execution.get("entity_type") or "").strip().lower()
        action = self._resolve_github_writeback_action(execution, update)
        review_events = {
            "approved": "APPROVE",
            "reviewed": "COMMENT",
            "changes_requested": "REQUEST_CHANGES",
        }
        review_action = entity_type == "pr" and action in review_events
        self._record_github_writeback_attempt(action, update.detail)
        review_result: Optional[dict[str, Any]] = None
        if review_action:
            snapshot = self._build_review_snapshot(execution, update.detail)
            suppress, reason = self._classify_review_writeback_detail(
                execution,
                update.detail,
                snapshot=snapshot,
                action=action,
            )
            score, reasons = self._score_review_quality(snapshot, action=action)
            self._record_review_quality(score, reasons)
            if not suppress and action == "approved":
                reason = self._approval_quality_failure(snapshot, score)
                suppress = reason is not None
            if suppress:
                review_result = self._build_review_trial_result(
                    execution,
                    update,
                    attempted_action=action,
                    resolved_action="suppressed",
                    review_action=review_action,
                    snapshot=snapshot,
                    score=score,
                    reasons=reasons,
                    suppression_reason=reason,
                )
                self._record_github_writeback_suppression(
                    bridge_id=str(execution.get("bridge_id") or update.bridge_id),
                    action=action,
                    detail=update.detail,
                    reason=reason,
                )
                return "suppressed", reason, review_result
        body = self._render_github_writeback_body(
            str(execution.get("bridge_id") or update.bridge_id),
            action,
            update.detail,
            target_alias=str(execution.get("target_alias") or "").strip() or None,
        )
        if review_action:
            self._submit_github_review(repo_full_name, number, review_events[action], body, github_token)
            self._record_github_writeback_publish(action, review_action=True)
            review_result = self._build_review_trial_result(
                execution,
                update,
                attempted_action=action,
                resolved_action=action,
                review_action=review_action,
                snapshot=snapshot,
                score=score,
                reasons=reasons,
                suppression_reason=None,
            )
            return action, None, review_result
        self._post_github_comment(repo_full_name, number, body, github_token)
        self._record_github_writeback_publish("commented", review_action=False)
        return "commented", None, None

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
            resolved_action, suppression_reason, review_result = self._write_back_to_github(refreshed, update)
            if resolved_action == "suppressed":
                retry_count = int(refreshed.get("retry_count") or 0)
                if retry_count < 2 and self._suppression_reason_allows_retry(suppression_reason):  # MAX_RETRIES = 2
                    retry_queued = await self._issue_retry_task(refreshed, suppression_reason)
                    if retry_queued:
                        resolved_action = "retrying"
            if resolved_action != update.action:
                self.store.update_execution(update.bridge_id, action=resolved_action)
                refreshed = self.store.get_execution(update.bridge_id) or refreshed
            if review_result is not None:
                review_result["resolved_action"] = resolved_action
                review_result["retry_queued"] = resolved_action == "retrying"
                review_result["retry_count"] = int(refreshed.get("retry_count") or 0)
                self.store.update_execution(update.bridge_id, review_result=review_result)
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

    def list_review_trials(self, *, limit: int = 20) -> dict[str, Any]:
        items = self.store.list_recent_review_trials(limit=limit)
        return {"status": "ok", "count": len(items), "items": items}

    async def _issue_retry_task(self, execution: dict[str, Any], reason: Optional[str]) -> bool:
        bridge_id = str(execution["bridge_id"])
        context_id = str(execution["context_id"])
        target_node_id = str(execution["target_node_id"])
        target_alias = str(execution["target_alias"])
        retry_count = int(execution.get("retry_count") or 0) + 1
        github_inputs = self._execution_github_inputs(execution)

        # Build critique instructions
        critique = f"Your previous review was suppressed because: {reason or 'weak_output'}.\n"
        if reason == "summary_without_code_evidence":
            critique += "Please provide a more detailed review with concrete code identifiers (variables, functions) found in the actual PR diff."
        elif reason == "summary_without_risk_coverage":
            critique += "You summarized the diff without stating which risk areas or checks you actually covered. Re-review the PR and list the concrete risk areas checked and checks performed."
        elif reason == "summary_without_changed_behavior_evidence":
            critique += "Your why-no-finding explanation mentioned code behavior without grounding it in changed-line identifiers. Re-check the actual diff and cite the changed behavior you verified."
        elif reason in ("finding_in_context_only", "observation_in_context_only"):
            critique += "The code identifiers you mentioned are in the file context but NOT in the actual changed lines (+/-). Please focus your review on the actual changes."
        elif reason == "approval_without_changed_code_evidence":
            critique += "You attempted to approve the PR without citing changed-line code evidence. Reference concrete identifiers from the actual diff before approving."
        elif reason == "approval_without_test_awareness":
            critique += "You attempted to approve the PR without acknowledging the relevant changed tests. Re-check the diff and mention the test coverage you relied on."
        elif reason == "approval_below_quality_bar":
            critique += "You attempted to approve the PR with insufficient evidence density. Add concrete changed-line evidence and test/risk coverage before approving."
        elif reason == "approval_checks_pending":
            critique += "You attempted to approve the PR before its GitHub checks finished. Do not approve while CI is still pending or running."
        elif reason == "approval_checks_not_green":
            critique += "You attempted to approve the PR even though one or more GitHub checks are failing. Do not approve until the checks are green."
        else:
            critique += "Please re-examine the PR diff and provide a higher-quality review anchored to the actual changes."
            
        new_sequence = self.store.next_event_sequence(context_id)
        
        # Reconstruct event enough for envelope
        event = NormalizedGitHubEvent(
            delivery_id="",
            source_event="retry",
            source_action="retry",
            repo_full_name=str(execution["repo_full_name"]),
            entity_type=str(execution["entity_type"]),
            number=int(execution["issue_number"]),
            title="",
            url="",
            actor_login="",
            author_association="",
            context_id=context_id,
            imperative_verb=str(execution["imperative_verb"]),
            intent_type=str(execution["intent_type"]),
            instructions=f"{critique}\n\nOriginal instructions follow:\n{execution.get('instructions', '')}",
            raw_trigger_text="",
            github_inputs=github_inputs,
            event_sequence=new_sequence,
            bridge_id=bridge_id,
        )

        envelope = self._build_interbot_envelope(
            event, target_node_id=target_node_id,
            target_alias=target_alias, bridge_id=bridge_id,
        )

        try:
            response = await asyncio.to_thread(
                self.submission_client.submit_structured_dm,
                envelope,
                target_node_id,
                event.intent_type,
            )
            status_code = int(response.get("status_code") or 500) if isinstance(response, dict) else 500
            payload = response.get("json") if isinstance(response, dict) else None
            if status_code >= 400 or not isinstance(payload, dict):
                return False
            task_id = payload.get("task_id")
            execution_status = str(payload.get("status") or "submitted")
            self.store.update_execution(
                bridge_id,
                status=execution_status,
                task_id=str(task_id) if task_id else None,
                retry_count=retry_count,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

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

    @app.get("/bridge/review-trials")
    async def bridge_review_trials(limit: int = 20) -> dict[str, Any]:
        return bridge_service.list_review_trials(limit=limit)

    return app


app = create_app()
