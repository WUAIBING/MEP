from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, Header, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import asyncio
import uuid
import time
import json
import ipaddress
from datetime import datetime
import os
import ctypes
from urllib.parse import urlparse, urlencode
from urllib.request import Request as UrlRequest, urlopen
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import db
import auth
from logger import log_event, log_audit

from models import (
    NodeRegistration,
    TaskCreate,
    TaskResult,
    TaskBid,
    TaskCancel,
    RegistryUpdate,
    AvailabilityUpdate,
    RegistryHeartbeat,
    ReputationSubmit,
    DisputeOpen,
    DisputeResolve,
    FederationPeerUpsert,
    MeshAssembleRequest,
    BrainstormSessionCreate,
    BrainstormSessionPost,
)

app = FastAPI(title="MEP Hub", description="The Time Exchange Clearinghouse", version="0.1.2")

# In-memory storage for active tasks
active_tasks: Dict[str, dict] = {}
completed_tasks: Dict[str, dict] = {}
connected_nodes: Dict[str, WebSocket] = {}
rate_limits: Dict[str, List[float]] = {}
task_lock = asyncio.Lock()
node_lock = asyncio.Lock()
mesh_lock = asyncio.Lock()
anti_loop_lock = asyncio.Lock()
brainstorm_lock = asyncio.Lock()
mesh_assemblies: Dict[str, dict] = {}
anti_loop_channels: Dict[str, dict] = {}
brainstorm_sessions: Dict[str, dict] = {}
onboard_diagnosis_counts: Dict[str, int] = {}
onboard_diagnosis_total = 0
MAX_BODY_BYTES = 200_000
MAX_PAYLOAD_CHARS = 20_000
RATE_LIMIT_WINDOW = 10.0
RATE_LIMIT_MAX = 50

def get_hub_urls(request: Request) -> tuple:
    """Get correct Hub and WebSocket URLs for clients."""
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower().strip()
    base_url = str(request.base_url).rstrip("/")
    if TRUST_PROXY_PROTO and forwarded_proto in ("http", "https"):
        base_url = base_url.replace("http://", f"{forwarded_proto}://", 1).replace("https://", f"{forwarded_proto}://", 1)
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
    return base_url, ws_url
MAX_SKEW_SECONDS = 300
ALLOWED_IPS = [ip.strip() for ip in os.getenv("MEP_ALLOWED_IPS", "").split(",") if ip.strip()]
REQUIRE_TLS = os.getenv("MEP_REQUIRE_TLS", "true").lower() in ("1", "true", "yes")
TRUST_PROXY_PROTO = os.getenv("MEP_TRUST_PROXY_PROTO", "true").lower() in ("1", "true", "yes")
TRUST_PROXY_CLIENT_IP = os.getenv("MEP_TRUST_PROXY_CLIENT_IP", "false").lower() in ("1", "true", "yes")
TRUSTED_HOSTS = {
    item.strip().lower()
    for item in os.getenv("MEP_TRUSTED_HOSTS", "").split(",")
    if item.strip()
}


def _build_trusted_host_rules(values: set[str]) -> tuple[set[str], list[str]]:
    exact: set[str] = set()
    wildcard_suffixes: list[str] = []
    for value in values:
        normalized = value.strip().lower().strip(".")
        if not normalized:
            continue
        if normalized.startswith("*.") and len(normalized) > 2:
            wildcard_suffixes.append(normalized[2:])
            continue
        exact.add(normalized)
    return exact, wildcard_suffixes


TRUSTED_HOSTS_EXACT, TRUSTED_HOSTS_WILDCARD_SUFFIXES = _build_trusted_host_rules(TRUSTED_HOSTS)


def _build_allowed_ip_rules(values: list[str]) -> tuple[set[str], list]:
    exact: set[str] = set()
    networks: list = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        try:
            if "/" in normalized:
                networks.append(ipaddress.ip_network(normalized, strict=False))
                continue
            exact.add(str(ipaddress.ip_address(normalized)))
            continue
        except ValueError:
            exact.add(normalized.lower())
    return exact, networks


ALLOWED_IP_EXACT, ALLOWED_IP_NETWORKS = _build_allowed_ip_rules(ALLOWED_IPS)
REQUEUE_ASSIGNED_ON_START = os.getenv("MEP_REQUEUE_ASSIGNED_ON_START", "false").lower() in ("1", "true", "yes")
ADMIN_KEY = os.getenv("MEP_ADMIN_KEY")
DISPUTE_WINDOW_SECONDS = int(os.getenv("MEP_DISPUTE_WINDOW_SECONDS", "86400"))
DISPUTE_REASON_MIN_CHARS = int(os.getenv("MEP_DISPUTE_REASON_MIN_CHARS", "10"))
DISPUTE_REASON_MAX_CHARS = int(os.getenv("MEP_DISPUTE_REASON_MAX_CHARS", "500"))
ASSIGNMENT_TIMEOUT_SECONDS = int(os.getenv("MEP_ASSIGNMENT_TIMEOUT_SECONDS", "3600"))
TASK_TIMEOUT_DEFAULT_SECONDS = int(os.getenv("MEP_TASK_TIMEOUT_DEFAULT_SECONDS", str(ASSIGNMENT_TIMEOUT_SECONDS)))
TASK_TIMEOUT_MIN_SECONDS = int(os.getenv("MEP_TASK_TIMEOUT_MIN_SECONDS", "60"))
TASK_TIMEOUT_MAX_SECONDS = int(os.getenv("MEP_TASK_TIMEOUT_MAX_SECONDS", "86400"))
ASSIGNMENT_SWEEP_INTERVAL_SECONDS = int(os.getenv("MEP_ASSIGNMENT_SWEEP_INTERVAL_SECONDS", "60"))
MAINTENANCE_SWEEP_INTERVAL_SECONDS = int(os.getenv("MEP_MAINTENANCE_SWEEP_INTERVAL_SECONDS", "60"))
REGISTRY_RECONCILE_LIMIT = int(os.getenv("MEP_REGISTRY_RECONCILE_LIMIT", "1000"))
REGISTRY_RECONCILE_STALE_SECONDS = float(os.getenv("MEP_REGISTRY_RECONCILE_STALE_SECONDS", "180"))
COMPLETED_TASK_CACHE_TTL_SECONDS = int(os.getenv("MEP_COMPLETED_TASK_CACHE_TTL_SECONDS", "3600"))
COMPLETED_TASK_CACHE_MAX_ITEMS = int(os.getenv("MEP_COMPLETED_TASK_CACHE_MAX_ITEMS", "1000"))
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("MEP_IDEMPOTENCY_TTL_SECONDS", "86400"))
TIMEOUT_POLICY = os.getenv("MEP_TIMEOUT_POLICY", "refund").lower()
VALID_AVAILABILITY = {"online", "idle", "busy", "offline", "degraded", "unknown"}
LIVE_AVAILABILITY = {"online", "idle", "busy"}
INTERBOT_SPEC_VERSION = "mep.interbot.v1"
INTERBOT_ALLOWED_INTENTS = {
    "chat.request",
    "coordination.request",
    "deployment.request",
    "analysis.request",
    "code.review.request",
    "incident.response",
    "test.request",
    "human.approval.request",
    "human.input.request",
    "data.feed.offer",
    "data.dataset.offer",
}
INTERBOT_SPEC_VALIDATE_ENABLED = os.getenv("MEP_INTERBOT_SPEC_VALIDATE", "false").lower() in ("1", "true", "yes")
INTERBOT_CLOCK_SKEW_MS = int(os.getenv("MEP_INTERBOT_CLOCK_SKEW_MS", "600000"))
INTERBOT_LEGACY_POLICY = os.getenv("MEP_INTERBOT_LEGACY_POLICY", "dm_only").strip().lower() or "dm_only"
MESH_ALLOWED_TRIGGERS = {"brainstorm", "code_review", "incident", "planning"}
MESH_DEFAULT_TIMEOUT_SECONDS = 300
MESH_MAX_TIMEOUT_SECONDS = 3600
ANTI_LOOP_ENABLED = os.getenv("MEP_ANTI_LOOP_ENABLED", "true").lower() in ("1", "true", "yes")
ANTI_LOOP_WINDOW_SECONDS = float(os.getenv("MEP_ANTI_LOOP_WINDOW_SECONDS", "90"))
ANTI_LOOP_MAX_MESSAGES = int(os.getenv("MEP_ANTI_LOOP_MAX_MESSAGES", "6"))
ANTI_LOOP_COOLDOWN_SECONDS = int(os.getenv("MEP_ANTI_LOOP_COOLDOWN_SECONDS", "60"))
ANTI_LOOP_TERMINATION_TOKENS = ("[end]", "[no_relay]", "[ack_only]")

if TASK_TIMEOUT_MIN_SECONDS > TASK_TIMEOUT_MAX_SECONDS:
    TASK_TIMEOUT_MIN_SECONDS, TASK_TIMEOUT_MAX_SECONDS = TASK_TIMEOUT_MAX_SECONDS, TASK_TIMEOUT_MIN_SECONDS
TASK_TIMEOUT_DEFAULT_SECONDS = max(TASK_TIMEOUT_MIN_SECONDS, min(TASK_TIMEOUT_DEFAULT_SECONDS, TASK_TIMEOUT_MAX_SECONDS))

# ---------------------------------------------------------------------------
# Node Activity Tracking (for passive degraded detection)
# ---------------------------------------------------------------------------
node_activity_lock = asyncio.Lock()
node_last_activity: Dict[str, float] = {}
registry_reconcile_lock = asyncio.Lock()
registry_reconcile_metrics: Dict[str, Any] = {
    "runs": 0,
    "candidates_scanned": 0,
    "reconciled_total": 0,
    "skipped_recent_total": 0,
    "last_run_at": None,
    "last_run_candidates": 0,
    "last_run_reconciled": 0,
    "last_run_skipped_recent": 0,
    "last_reconciled_at": None,
}

DEGRADED_THRESHOLD_SECONDS = float(os.getenv("MEP_DEGRADED_THRESHOLD_SECONDS", "600"))


async def _track_node_activity(node_id: str):
    async with node_activity_lock:
        node_last_activity[node_id] = time.time()


async def _sweep_node_activity():
    now = time.time()
    async with node_activity_lock:
        for node_id, last_seen in list(node_last_activity.items()):
            if node_id in connected_nodes:
                continue
            if (now - last_seen) > DEGRADED_THRESHOLD_SECONDS:
                existing = db.get_registry(node_id)
                if existing and existing.get("availability") not in ("offline", "degraded"):
                    db.update_registry_availability(node_id, "degraded", now)
                    log_event("node_degraded", f"Node {node_id} marked degraded due to inactivity", node_id=node_id)


async def _reconcile_live_registry_presence():
    stale_candidates: list[tuple[str, str]] = []
    for availability in LIVE_AVAILABILITY:
        rows = db.search_registry(
            alias=None,
            skill=None,
            model=None,
            availability=availability,
            min_score=None,
            min_reviews=None,
            min_updated_at=None,
            limit=max(1, REGISTRY_RECONCILE_LIMIT),
        )
        for row in rows:
            node_id = str(row.get("node_id") or "").strip()
            if not node_id:
                continue
            stale_candidates.append((node_id, availability))

    if not stale_candidates:
        now = time.time()
        async with registry_reconcile_lock:
            registry_reconcile_metrics["runs"] += 1
            registry_reconcile_metrics["last_run_at"] = now
            registry_reconcile_metrics["last_run_candidates"] = 0
            registry_reconcile_metrics["last_run_reconciled"] = 0
            registry_reconcile_metrics["last_run_skipped_recent"] = 0
        return

    now = time.time()
    reconciled = 0
    skipped_recent = 0
    for node_id, seen_availability in stale_candidates:
        async with node_lock:
            if node_id in connected_nodes:
                continue
        existing = db.get_registry(node_id)
        if not existing:
            continue
        current_availability = str(existing.get("availability") or "unknown").strip().lower()
        if current_availability not in LIVE_AVAILABILITY:
            continue
        last_updated = existing.get("updated_at")
        if last_updated is not None:
            try:
                age_seconds = now - float(last_updated)
                if age_seconds < REGISTRY_RECONCILE_STALE_SECONDS:
                    skipped_recent += 1
                    continue
            except (TypeError, ValueError):
                pass
        db.update_registry_availability(node_id, "offline", now)
        reconciled += 1
        log_event(
            "registry_reconciled_offline",
            f"Node {node_id} forced offline (registry={seen_availability}, ws=disconnected)",
            node_id=node_id,
            previous_availability=seen_availability,
            reconciled_availability="offline",
        )
    async with registry_reconcile_lock:
        registry_reconcile_metrics["runs"] += 1
        registry_reconcile_metrics["candidates_scanned"] += len(stale_candidates)
        registry_reconcile_metrics["reconciled_total"] += reconciled
        registry_reconcile_metrics["skipped_recent_total"] += skipped_recent
        registry_reconcile_metrics["last_run_at"] = now
        registry_reconcile_metrics["last_run_candidates"] = len(stale_candidates)
        registry_reconcile_metrics["last_run_reconciled"] = reconciled
        registry_reconcile_metrics["last_run_skipped_recent"] = skipped_recent
        if reconciled:
            registry_reconcile_metrics["last_reconciled_at"] = now
    if reconciled:
        log_event("registry_reconcile_summary", f"Reconciled {reconciled} stale live registry entries", reconciled=reconciled)
DEFAULT_REGISTRY_MAX_AGE_MINUTES = float(os.getenv("MEP_REGISTRY_MAX_AGE_MINUTES", "0") or "0")
ASSIGNMENT_REPUTATION_WEIGHT = float(os.getenv("MEP_ASSIGNMENT_REPUTATION_WEIGHT", "0.55"))
ASSIGNMENT_AVAILABILITY_WEIGHT = float(os.getenv("MEP_ASSIGNMENT_AVAILABILITY_WEIGHT", "0.25"))
ASSIGNMENT_CAPABILITY_WEIGHT = float(os.getenv("MEP_ASSIGNMENT_CAPABILITY_WEIGHT", "0.20"))
ASSIGNMENT_REPUTATION_CONFIDENCE_REVIEWS = int(os.getenv("MEP_ASSIGNMENT_REPUTATION_CONFIDENCE_REVIEWS", "10"))
RISK_MIN_REPUTATION_SCORE = float(os.getenv("MEP_RISK_MIN_REPUTATION_SCORE", "2.5"))
RISK_MIN_REPUTATION_REVIEWS = int(os.getenv("MEP_RISK_MIN_REPUTATION_REVIEWS", "3"))
RISK_REJECT_AVAILABILITY = {
    item.strip().lower()
    for item in os.getenv("MEP_RISK_REJECT_AVAILABILITY", "offline").split(",")
    if item.strip()
}
RFC_TOP_K = int(os.getenv("MEP_RFC_TOP_K", "0"))
HUB_ID = os.getenv("MEP_HUB_ID", "hub-0").strip() or "hub-0"
FEDERATION_ENABLED = os.getenv("MEP_FEDERATION_ENABLED", "false").lower() in ("1", "true", "yes")
FEDERATION_DISCOVERY_TIMEOUT_SECONDS = float(os.getenv("MEP_FEDERATION_DISCOVERY_TIMEOUT_SECONDS", "2.0"))
FEDERATION_REMOTE_LIMIT = int(os.getenv("MEP_FEDERATION_REMOTE_LIMIT", "20"))
FEDERATION_SEED_PEERS = {
    item.strip().rstrip("/")
    for item in os.getenv("MEP_FEDERATION_PEERS", "").split(",")
    if item.strip()
}
federation_peer_lock = asyncio.Lock()
dynamic_federation_peers: set[str] = set()


def _request_proto(request: Request) -> str:
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower().strip()
    if TRUST_PROXY_PROTO and forwarded_proto in ("http", "https"):
        return forwarded_proto
    return request.url.scheme.lower()


def _extract_client_ip(raw_client_host: Optional[str], forwarded_for: Optional[str]) -> Optional[str]:
    if TRUST_PROXY_CLIENT_IP and forwarded_for:
        first = forwarded_for.split(",", 1)[0]
        normalized_first = _normalize_client_endpoint(first)
        if normalized_first:
            return normalized_first
    return _normalize_client_endpoint(raw_client_host)


def _normalize_client_endpoint(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().lower().strip('"')
    if not normalized:
        return None
    if normalized.startswith("[") and "]" in normalized:
        return normalized[1:normalized.index("]")]
    parts = normalized.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit() and "." in parts[0]:
        return parts[0]
    return normalized


def _request_client_ip(request: Request) -> Optional[str]:
    raw_client_host = request.client.host if request.client else None
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    return _extract_client_ip(raw_client_host, forwarded_for)


def _websocket_client_ip(websocket: WebSocket) -> Optional[str]:
    raw_client_host = websocket.client.host if websocket.client else None
    forwarded_for = websocket.headers.get("X-Forwarded-For", "")
    return _extract_client_ip(raw_client_host, forwarded_for)


def _normalize_host_header(host_value: Optional[str]) -> Optional[str]:
    if not host_value:
        return None
    normalized = host_value.strip().lower()
    if normalized.startswith("[") and "]" in normalized:
        return normalized[1:normalized.index("]")]
    return normalized.split(":", 1)[0]


def _is_trusted_host(host_value: Optional[str]) -> bool:
    if not TRUSTED_HOSTS:
        return True
    normalized = _normalize_host_header(host_value)
    if not normalized:
        return False
    normalized = normalized.rstrip(".")
    if normalized in TRUSTED_HOSTS_EXACT:
        return True
    return any(normalized.endswith(f".{suffix}") for suffix in TRUSTED_HOSTS_WILDCARD_SUFFIXES)


def _is_local_dev_host(host_value: Optional[str]) -> bool:
    normalized = _normalize_host_header(host_value)
    if not normalized:
        return False
    return normalized in {"localhost", "127.0.0.1", "::1", "testserver"}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    if not _is_trusted_host(request.headers.get("host")):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Untrusted Host header"},
        )
    proto = _request_proto(request)
    if REQUIRE_TLS and proto != "https" and not _is_local_dev_host(request.headers.get("host")):
        return JSONResponse(
            status_code=426,
            content={"status": "error", "detail": "TLS required. Use HTTPS/WSS via reverse proxy."},
        )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

def _normalize_error_detail(detail):
    if isinstance(detail, (str, list, dict)):
        return detail
    return str(detail)

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "detail": _normalize_error_detail(exc.detail)}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"status": "error", "detail": exc.errors()}
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    log_event("unhandled_exception", str(exc))
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": "Internal server error"}
    )

async def _load_active_tasks_from_db():
    active_db_tasks = db.get_active_tasks()
    if REQUEUE_ASSIGNED_ON_START and active_db_tasks:
        now = time.time()
        for task in active_db_tasks:
            if task["status"] == "assigned" and not task["target_node"]:
                db.update_task_status(task["task_id"], "bidding", now)
                log_event("task_requeued", f"Task {task['task_id'][:8]} requeued on startup", consumer_id=task["consumer_id"], task_id=task["task_id"], bounty=task["bounty"])
    loaded_tasks = {}
    for task in active_db_tasks:
        task_data = {
            "id": task["task_id"],
            "consumer_id": task["consumer_id"],
            "payload": task["payload"],
            "bounty": task["bounty"],
            "status": task["status"],
            "target_node": task["target_node"],
            "model_requirement": task["model_requirement"],
            "expires_in_seconds": task.get("expires_in_seconds"),
            "provider_id": task["provider_id"],
            "payload_uri": task.get("payload_uri"),
            "secret_data": task.get("result_payload")
        }
        loaded_tasks[task_data["id"]] = task_data
    async with task_lock:
        active_tasks.clear()
        active_tasks.update(loaded_tasks)

# --- IDENTITY VERIFICATION MIDDLEWARE ---
def _is_allowed_ip(host: Optional[str]) -> bool:
    if not ALLOWED_IPS:
        return True
    normalized = _normalize_client_endpoint(host)
    if not normalized:
        return False
    if normalized in ALLOWED_IP_EXACT:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(address in network for network in ALLOWED_IP_NETWORKS)

def _apply_rate_limit(key: str):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = rate_limits.get(key, [])
    timestamps = [t for t in timestamps if t >= window_start]
    if len(timestamps) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    timestamps.append(now)
    rate_limits[key] = timestamps

def _require_admin(x_mep_admin_key: Optional[str]):
    if not ADMIN_KEY or not x_mep_admin_key or x_mep_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required")

def _normalize_availability(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_AVAILABILITY:
        raise HTTPException(status_code=400, detail="Invalid availability")
    return normalized

def _normalize_model_requirement(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    return normalized


def _task_consumer_id(task: TaskCreate) -> str:
    if task.consumer_id:
        return task.consumer_id
    source = task.source if isinstance(task.source, dict) else {}
    node_id = source.get("node_id") if isinstance(source, dict) else None
    if isinstance(node_id, str) and node_id.strip():
        return node_id.strip()
    raise HTTPException(status_code=400, detail="Task requires consumer_id or source.node_id")


def _task_instructions(task: TaskCreate) -> str:
    if isinstance(task.task, dict):
        instructions = task.task.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            return instructions.strip()
    if isinstance(task.payload, str) and task.payload.strip():
        return task.payload.strip()
    raise HTTPException(status_code=400, detail="Task requires payload or task.instructions")


def _task_expected_output(task: TaskCreate) -> dict:
    if isinstance(task.task, dict):
        expected_output = task.task.get("expected_output")
        if isinstance(expected_output, dict):
            return expected_output
    return {}


def _task_economics(task: TaskCreate) -> dict:
    if isinstance(task.economics, dict):
        economics = dict(task.economics)
        currency_raw = economics.get("currency")
        currency = str(currency_raw or "").strip().upper() if currency_raw is not None else None
        market = str(economics.get("market") or "").strip().lower() or None
        payment_direction = str(economics.get("payment_direction") or "").strip().lower() or None

        bounty_quanta_value = economics.get("bounty_quanta")
        if bounty_quanta_value is not None:
            if currency is None:
                currency = "MEP_QUANTA"
            if currency != "MEP_QUANTA":
                raise HTTPException(status_code=400, detail="Task economics currency must be MEP_QUANTA")
            if isinstance(bounty_quanta_value, bool):
                raise HTTPException(status_code=400, detail="Task economics bounty_quanta must be an integer")
            if isinstance(bounty_quanta_value, int):
                bounty_quanta = bounty_quanta_value
            elif isinstance(bounty_quanta_value, str) and bounty_quanta_value.strip().isdigit():
                bounty_quanta = int(bounty_quanta_value.strip())
            else:
                raise HTTPException(status_code=400, detail="Task economics bounty_quanta must be an integer or decimal string")
            if bounty_quanta < 0:
                raise HTTPException(status_code=400, detail="Task economics bounty_quanta must be non-negative")
            bounty_seconds_unsigned = float(bounty_quanta) / 1_000_000_000.0

            if market is None:
                market = "chat" if bounty_quanta == 0 else "compute"
            if payment_direction is None:
                payment_direction = "none" if market == "chat" else ("receiver_to_sender" if market == "data" else "sender_to_receiver")

            if market == "chat":
                if bounty_quanta != 0 or payment_direction != "none":
                    raise HTTPException(status_code=400, detail="Task economics chat market requires bounty_quanta=0 and payment_direction=none")
                bounty_seconds = 0.0
            elif market == "compute":
                if bounty_quanta <= 0 or payment_direction != "sender_to_receiver":
                    raise HTTPException(status_code=400, detail="Task economics compute market requires bounty_quanta>0 and payment_direction=sender_to_receiver")
                bounty_seconds = bounty_seconds_unsigned
            elif market == "data":
                if bounty_quanta <= 0 or payment_direction != "receiver_to_sender":
                    raise HTTPException(status_code=400, detail="Task economics data market requires bounty_quanta>0 and payment_direction=receiver_to_sender")
                bounty_seconds = -bounty_seconds_unsigned
            else:
                raise HTTPException(status_code=400, detail="Task economics market must be compute, chat, or data")

            return {
                "bounty_quanta": bounty_quanta,
                "bounty_seconds": bounty_seconds,
                "market": market,
                "payment_direction": payment_direction,
                "currency": "MEP_QUANTA",
            }

        bounty_value = economics.get("bounty_seconds")
        if bounty_value is None:
            bounty_value = economics.get("bounty")
        if bounty_value is None:
            bounty_value = economics.get("bounty_ns")
        if bounty_value is None:
            bounty_value = task.bounty if task.bounty is not None else task.bounty_ns
        if bounty_value is None:
            bounty_value = 0.0
        try:
            bounty_seconds = float(bounty_value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Task economics bounty must be a number")
        if market is None:
            market = "data" if bounty_seconds < 0 else ("chat" if bounty_seconds == 0 else "compute")
            payment_direction = payment_direction or ("receiver_to_sender" if market == "data" else ("none" if market == "chat" else "sender_to_receiver"))
        if payment_direction is None:
            payment_direction = "sender_to_receiver"
        return {
            "bounty_seconds": bounty_seconds,
            "market": market,
            "payment_direction": payment_direction,
            "currency": (currency or "SECONDS"),
        }
    bounty_value = task.bounty if task.bounty is not None else task.bounty_ns
    if bounty_value is None:
        bounty_value = 0.0
    try:
        bounty_seconds = float(bounty_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Task bounty must be a number")
    market = "data" if bounty_seconds < 0 else ("chat" if bounty_seconds == 0 else "compute")
    payment_direction = "receiver_to_sender" if bounty_seconds < 0 else ("none" if bounty_seconds == 0 else "sender_to_receiver")
    return {
        "bounty_seconds": bounty_seconds,
        "market": market,
        "payment_direction": payment_direction,
        "currency": "SECONDS",
    }


def _task_target_node(task: TaskCreate) -> Optional[str]:
    if isinstance(task.routing, dict):
        target_node_id = task.routing.get("target_node_id")
        if isinstance(target_node_id, str) and target_node_id.strip():
            return target_node_id.strip()
        legacy_target = task.routing.get("target_node")
        if isinstance(legacy_target, str) and legacy_target.strip():
            return legacy_target.strip()
    if isinstance(task.target_node, str) and task.target_node.strip():
        return task.target_node.strip()
    return None


def _task_target_capability(task: TaskCreate) -> Optional[str]:
    if isinstance(task.routing, dict):
        capability = task.routing.get("target_capability")
        if isinstance(capability, str) and capability.strip():
            return capability.strip()
    if isinstance(task.model_requirement, str) and task.model_requirement.strip():
        return task.model_requirement.strip()
    return None


async def _coerce_live_availability(node_id: str, availability: Optional[str]) -> Optional[str]:
    if availability is None or availability not in LIVE_AVAILABILITY:
        return availability
    async with node_lock:
        has_live_websocket = node_id in connected_nodes
    if has_live_websocket:
        return availability
    return "offline"


def _interbot_require_object(data: Any, field_name: str) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail=f"Inter-bot payload missing object: {field_name}")
    return data


def _interbot_require_string(data: dict, field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"Inter-bot payload missing field: {field_name}")
    return value.strip()


def _validate_interbot_payload_if_enabled(consumer_id: str, task_economics: dict, target_node: Optional[str], payload: str) -> None:
    if not INTERBOT_SPEC_VALIDATE_ENABLED or not payload:
        return
    payload_text = payload.strip()
    if not payload_text:
        return

    try:
        message = json.loads(payload_text)
    except json.JSONDecodeError:
        bounty_seconds = float(task_economics.get("bounty_seconds") or 0.0)
        if INTERBOT_LEGACY_POLICY == "dm_only" and not (target_node and bounty_seconds == 0.0):
            raise HTTPException(
                status_code=400,
                detail="Legacy plaintext payload allowed only for direct DM tasks when MEP_INTERBOT_SPEC_VALIDATE=true",
            )
        return

    message_obj = _interbot_require_object(message, "root")
    if message_obj.get("spec_version") != INTERBOT_SPEC_VERSION:
        raise HTTPException(status_code=400, detail="Inter-bot payload spec_version must be mep.interbot.v1")
    _interbot_require_string(message_obj, "message_id")
    timestamp_ms = message_obj.get("timestamp_ms")
    if not isinstance(timestamp_ms, (int, float)):
        raise HTTPException(status_code=400, detail="Inter-bot payload missing field: timestamp_ms")
    now_ms = int(time.time() * 1000)
    if abs(now_ms - int(timestamp_ms)) > INTERBOT_CLOCK_SKEW_MS:
        raise HTTPException(status_code=400, detail="Inter-bot payload timestamp_ms out of allowed window")

    source_obj = _interbot_require_object(message_obj.get("source"), "source")
    source_node_id = _interbot_require_string(source_obj, "node_id")
    if source_node_id != consumer_id:
        raise HTTPException(status_code=400, detail="Inter-bot payload source.node_id mismatch with consumer_id")

    intent_obj = _interbot_require_object(message_obj.get("intent"), "intent")
    intent_type = _interbot_require_string(intent_obj, "type")
    if intent_type not in INTERBOT_ALLOWED_INTENTS and "." not in intent_type:
        raise HTTPException(status_code=400, detail="Inter-bot payload intent.type must use known type or namespaced custom value")

    task_obj = _interbot_require_object(message_obj.get("task"), "task")
    instructions = _interbot_require_string(task_obj, "instructions")
    if len(instructions) > 4000:
        raise HTTPException(status_code=400, detail="Inter-bot payload task.instructions too long (max 4000)")
    expected_output_obj = _interbot_require_object(task_obj.get("expected_output"), "task.expected_output")
    _interbot_require_string(expected_output_obj, "result_type")

    economics_obj = _interbot_require_object(message_obj.get("economics"), "economics")
    currency = economics_obj.get("currency")
    if currency != "MEP_QUANTA":
        raise HTTPException(status_code=400, detail="Inter-bot payload economics.currency must be MEP_QUANTA")
    bounty_quanta_value = economics_obj.get("bounty_quanta")
    if bounty_quanta_value is None:
        raise HTTPException(status_code=400, detail="Inter-bot payload missing field: economics.bounty_quanta")
    if isinstance(bounty_quanta_value, bool):
        raise HTTPException(status_code=400, detail="Inter-bot payload economics.bounty_quanta must be an integer")
    if isinstance(bounty_quanta_value, int):
        bounty_quanta = bounty_quanta_value
    elif isinstance(bounty_quanta_value, str) and bounty_quanta_value.strip().isdigit():
        bounty_quanta = int(bounty_quanta_value.strip())
    else:
        raise HTTPException(status_code=400, detail="Inter-bot payload economics.bounty_quanta must be an integer or decimal string")
    if bounty_quanta < 0:
        raise HTTPException(status_code=400, detail="Inter-bot payload economics.bounty_quanta must be non-negative")

    market = economics_obj.get("market")
    payment_direction = economics_obj.get("payment_direction")
    if market not in ("compute", "chat", "data"):
        raise HTTPException(status_code=400, detail="Inter-bot payload economics.market must be compute, chat, or data")
    if payment_direction not in ("sender_to_receiver", "receiver_to_sender", "none"):
        raise HTTPException(status_code=400, detail="Inter-bot payload economics.payment_direction invalid")
    if market == "chat" and (bounty_quanta != 0 or payment_direction != "none"):
        raise HTTPException(status_code=400, detail="Inter-bot payload economics chat market requires bounty_quanta=0 and payment_direction=none")
    if market == "compute" and (bounty_quanta <= 0 or payment_direction != "sender_to_receiver"):
        raise HTTPException(status_code=400, detail="Inter-bot payload economics compute market requires bounty_quanta>0 and payment_direction=sender_to_receiver")
    if market == "data" and (bounty_quanta <= 0 or payment_direction != "receiver_to_sender"):
        raise HTTPException(status_code=400, detail="Inter-bot payload economics data market requires bounty_quanta>0 and payment_direction=receiver_to_sender")

    expected_quanta = task_economics.get("bounty_quanta")
    if isinstance(expected_quanta, int):
        if bounty_quanta != expected_quanta:
            raise HTTPException(status_code=400, detail="Inter-bot payload economics.bounty_quanta must match task economics")
    else:
        bounty_seconds = float(task_economics.get("bounty_seconds") or 0.0)
        message_seconds_unsigned = float(bounty_quanta) / 1_000_000_000.0
        message_seconds = 0.0 if market == "chat" else (-message_seconds_unsigned if market == "data" else message_seconds_unsigned)
        if abs(float(message_seconds) - float(bounty_seconds)) > 1e-9:
            raise HTTPException(status_code=400, detail="Inter-bot payload economics must match task bounty")

    routing_obj = message_obj.get("routing")
    if isinstance(routing_obj, dict) and target_node:
        target_node_id = routing_obj.get("target_node_id")
        if isinstance(target_node_id, str) and target_node_id.strip() and target_node_id.strip() != target_node:
            raise HTTPException(status_code=400, detail="Inter-bot payload routing.target_node_id must match target_node")


def _direct_dm_channel(a: str, b: str) -> str:
    left, right = sorted([a, b])
    return f"dm:{left}:{right}"


def _contains_anti_loop_termination(payload: str) -> bool:
    lowered = (payload or "").lower()
    return any(token in lowered for token in ANTI_LOOP_TERMINATION_TOKENS)


async def _anti_loop_check_and_record(consumer_id: str, target_node: str, payload: str) -> None:
    if not ANTI_LOOP_ENABLED:
        return
    if not consumer_id or not target_node:
        return
    channel = _direct_dm_channel(consumer_id, target_node)
    now = time.time()
    async with anti_loop_lock:
        state = anti_loop_channels.setdefault(
            channel,
            {
                "events": [],
                "cooldown_until": 0.0,
                "circuit_break_count": 0,
                "last_reason": None,
                "last_break_at": None,
                "last_nodes": [],
            },
        )
        cooldown_until = float(state.get("cooldown_until", 0.0) or 0.0)
        if now < cooldown_until:
            remaining = int(max(1, cooldown_until - now))
            raise HTTPException(
                status_code=429,
                detail=f"Anti-loop cooldown active for {channel}; retry in {remaining}s",
            )

        state["events"].append(
            {
                "ts": now,
                "sender": consumer_id,
                "termination": _contains_anti_loop_termination(payload),
            }
        )
        cutoff = now - ANTI_LOOP_WINDOW_SECONDS
        state["events"] = [event for event in state["events"] if float(event.get("ts", 0)) >= cutoff]

        non_terminal_events = [event for event in state["events"] if not event.get("termination")]
        senders = [str(event.get("sender", "")) for event in non_terminal_events if event.get("sender")]
        unique_senders = sorted(set(senders))
        alternating = False
        if len(senders) >= 4:
            last4 = senders[-4:]
            alternating = (
                last4[0] == last4[2]
                and last4[1] == last4[3]
                and last4[0] != last4[1]
            )

        if len(non_terminal_events) >= ANTI_LOOP_MAX_MESSAGES and (alternating or len(unique_senders) <= 2):
            state["cooldown_until"] = now + ANTI_LOOP_COOLDOWN_SECONDS
            state["circuit_break_count"] = int(state.get("circuit_break_count", 0)) + 1
            state["last_reason"] = "bot_loop_detected"
            state["last_break_at"] = now
            state["last_nodes"] = unique_senders
            log_event(
                "anti_loop_circuit_break",
                f"Circuit break triggered for {channel}",
                channel=channel,
                nodes=unique_senders,
                cooldown_seconds=ANTI_LOOP_COOLDOWN_SECONDS,
            )
            raise HTTPException(
                status_code=429,
                detail=f"Anti-loop circuit break triggered for {channel}; cooldown {ANTI_LOOP_COOLDOWN_SECONDS}s",
            )


def _normalize_brainstorm_participants(owner_id: str, participants: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in participants:
        node_id = str(raw or "").strip()
        if not node_id:
            continue
        if node_id not in seen:
            cleaned.append(node_id)
            seen.add(node_id)
    if owner_id not in seen:
        cleaned.append(owner_id)
        seen.add(owner_id)
    return cleaned


def _normalize_mesh_trigger(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in MESH_ALLOWED_TRIGGERS:
        allowed = ", ".join(sorted(MESH_ALLOWED_TRIGGERS))
        raise HTTPException(status_code=400, detail=f"Invalid trigger. Allowed values: {allowed}")
    return normalized


def _normalize_mesh_timeout_seconds(value: Optional[int]) -> int:
    if value is None:
        return MESH_DEFAULT_TIMEOUT_SECONDS
    return max(1, min(int(value), MESH_MAX_TIMEOUT_SECONDS))


def _mesh_provider_rank(provider: Optional[str]) -> int:
    table = {
        "deepseek": 5,
        "yunwu": 4,
        "template": 3,
        "echo": 2,
    }
    if provider is None:
        return 1
    return table.get(provider.strip().lower(), 1)


def _mesh_role_score(role: str, candidate: dict) -> int:
    provider = candidate.get("ai_provider")
    ai_status = candidate.get("ai_status")
    thinking_mode = candidate.get("thinking_mode")
    preference = candidate.get("mesh_role_preference")
    availability = candidate.get("availability")
    connected = candidate.get("connected", False)

    score = _mesh_provider_rank(provider) * 10
    if ai_status == "online":
        score += 20
    elif ai_status == "degraded":
        score += 8
    elif ai_status == "offline":
        score -= 50
    if connected:
        score += 5

    if role == "strategist":
        if thinking_mode == "reasoning":
            score += 40
        if preference == "strategist":
            score += 20
    elif role == "implementer":
        if thinking_mode == "code_reading":
            score += 45
        if preference == "implementer":
            score += 20
        if provider in ("echo", None):
            score -= 15
    elif role == "facilitator":
        if thinking_mode == "aggregation":
            score += 35
        if preference == "facilitator":
            score += 20
        if availability in ("online", "idle"):
            score += 10
    elif role == "scout":
        if thinking_mode == "ack_only":
            score += 35
        if preference == "scout":
            score += 20
        if provider in ("echo", "template"):
            score += 15
    return score


def _mesh_role_payload(candidate: dict) -> dict:
    return {
        "node_id": candidate["node_id"],
        "alias": candidate.get("alias"),
        "ai_provider": candidate.get("ai_provider"),
        "status": candidate.get("availability"),
    }


def _pick_mesh_candidate(role: str, candidates: list[dict], used: set[str]) -> Optional[dict]:
    available = [item for item in candidates if item["node_id"] not in used]
    if not available:
        return None
    ranked = sorted(available, key=lambda item: (-_mesh_role_score(role, item), item["node_id"]))
    return ranked[0]


def _assign_mesh_roles(candidates: list[dict]) -> tuple[dict, list[str]]:
    roles: dict[str, dict] = {}
    missing: list[str] = []
    used: set[str] = set()
    for role in ("strategist", "implementer", "facilitator", "scout"):
        picked = _pick_mesh_candidate(role, candidates, used)
        if not picked:
            missing.append(role)
            continue
        used.add(picked["node_id"])
        roles[role] = _mesh_role_payload(picked)
    return roles, missing


async def _collect_mesh_candidates() -> list[dict]:
    query = _build_registry_query(None, None, None, None, None, None, None, 100)
    registry_entries = _search_registry_local(query)
    async with node_lock:
        connected_snapshot = set(connected_nodes.keys())
    candidates: list[dict] = []
    for entry in registry_entries:
        node_id = str(entry.get("node_id", "")).strip()
        if not node_id:
            continue
        availability = str(entry.get("availability") or "unknown").strip().lower()
        if availability == "offline":
            continue
        metadata = entry.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        candidate = {
            "node_id": node_id,
            "alias": entry.get("alias"),
            "availability": availability,
            "connected": node_id in connected_snapshot,
            "ai_provider": (str(metadata.get("ai_provider")).strip().lower() if metadata.get("ai_provider") is not None else None),
            "ai_status": str(metadata.get("ai_status") or "unknown").strip().lower(),
            "thinking_mode": str(metadata.get("thinking_mode") or "").strip().lower() or None,
            "mesh_role_preference": str(metadata.get("mesh_role_preference") or "").strip().lower() or None,
        }
        candidates.append(candidate)
    return candidates


async def _purge_expired_mesh_assemblies(now_ts: Optional[float] = None) -> None:
    now_ts = now_ts or time.time()
    async with mesh_lock:
        expired = [
            assembly_id
            for assembly_id, payload in mesh_assemblies.items()
            if payload.get("expires_at", 0.0) <= now_ts
        ]
        for assembly_id in expired:
            del mesh_assemblies[assembly_id]

def _normalize_artifact_uri(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.startswith("ipfs://"):
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be an http(s) or ipfs URI")
    return normalized

def _normalize_dispute_reason(reason: str) -> str:
    normalized = reason.strip()
    if len(normalized) < max(1, DISPUTE_REASON_MIN_CHARS):
        raise HTTPException(status_code=400, detail=f"Dispute reason must be at least {max(1, DISPUTE_REASON_MIN_CHARS)} characters")
    if len(normalized) > max(DISPUTE_REASON_MAX_CHARS, DISPUTE_REASON_MIN_CHARS):
        raise HTTPException(status_code=400, detail=f"Dispute reason must be at most {max(DISPUTE_REASON_MAX_CHARS, DISPUTE_REASON_MIN_CHARS)} characters")
    return normalized

def _normalize_hub_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="hub_url must be an http(s) URL")
    return normalized

def _validate_registration_pubkey(pubkey: str) -> str:
    raw_value = pubkey or ""
    if not raw_value.strip():
        raise HTTPException(status_code=400, detail="pubkey is required")
    if len(raw_value) > 8_192:
        raise HTTPException(status_code=413, detail="pubkey too large")
    try:
        public_key = serialization.load_pem_public_key(raw_value.encode("utf-8"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid PEM public key")
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise HTTPException(status_code=400, detail="Unsupported key type; expected Ed25519 public key")
    return raw_value

async def _list_federation_peers() -> list[str]:
    async with federation_peer_lock:
        peers = set(dynamic_federation_peers)
    peers.update(FEDERATION_SEED_PEERS)
    return sorted(peers)

def _build_registry_query(
    alias: Optional[str],
    skill: Optional[str],
    model: Optional[str],
    availability: Optional[str],
    min_score: Optional[float],
    min_reviews: Optional[int],
    max_age_minutes: Optional[float],
    limit: int
) -> dict:
    safe_limit = max(1, min(limit, 100))
    safe_min_score = min_score if min_score is None else max(0.0, min(min_score, 5.0))
    safe_min_reviews = min_reviews if min_reviews is None else max(0, min_reviews)
    if max_age_minutes is None:
        safe_max_age = DEFAULT_REGISTRY_MAX_AGE_MINUTES if DEFAULT_REGISTRY_MAX_AGE_MINUTES > 0 else None
    else:
        safe_max_age = max(0.0, max_age_minutes)
    min_updated_at = None if safe_max_age is None else time.time() - safe_max_age * 60.0
    normalized_alias = alias.strip().lower() if alias else None
    normalized_skill = skill.strip().lower() if skill else None
    normalized_model = model.strip().lower() if model else None
    normalized_availability = _normalize_availability(availability) if availability else None
    return {
        "safe_limit": safe_limit,
        "safe_min_score": safe_min_score,
        "safe_min_reviews": safe_min_reviews,
        "min_updated_at": min_updated_at,
        "normalized_alias": normalized_alias,
        "normalized_skill": normalized_skill,
        "normalized_model": normalized_model,
        "normalized_availability": normalized_availability
    }

def _search_registry_local(query: dict) -> list[dict]:
    return db.search_registry(
        query["normalized_alias"],
        query["normalized_skill"],
        query["normalized_model"],
        query["normalized_availability"],
        query["safe_min_score"],
        query["safe_min_reviews"],
        query["min_updated_at"],
        query["safe_limit"]
    )

def _fetch_peer_registry_results(peer_url: str, query: dict) -> list[dict]:
    params = {
        "limit": query["safe_limit"]
    }
    if query["normalized_alias"]:
        params["alias"] = query["normalized_alias"]
    if query["normalized_skill"]:
        params["skill"] = query["normalized_skill"]
    if query["normalized_model"]:
        params["model"] = query["normalized_model"]
    if query["normalized_availability"]:
        params["availability"] = query["normalized_availability"]
    if query["safe_min_score"] is not None:
        params["min_score"] = query["safe_min_score"]
    if query["safe_min_reviews"] is not None:
        params["min_reviews"] = query["safe_min_reviews"]
    if query["min_updated_at"] is not None:
        age_minutes = max(0.0, (time.time() - query["min_updated_at"]) / 60.0)
        params["max_age_minutes"] = round(age_minutes, 4)
    req = UrlRequest(f"{peer_url}/registry/search?{urlencode(params)}", headers={"Accept": "application/json"})
    with urlopen(req, timeout=max(0.5, FEDERATION_DISCOVERY_TIMEOUT_SECONDS)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return []
    results = payload.get("results", [])
    if not isinstance(results, list):
        return []
    normalized_results: list[dict] = []
    for item in results:
        if isinstance(item, dict):
            enriched = dict(item)
            enriched["source_hub"] = enriched.get("source_hub") or peer_url
            enriched["source_hub_url"] = enriched.get("source_hub_url") or peer_url
            normalized_results.append(enriched)
    return normalized_results

async def _discover_remote_registry(query: dict) -> tuple[list[dict], list[dict]]:
    if not FEDERATION_ENABLED:
        return [], []
    peer_urls = await _list_federation_peers()
    if not peer_urls:
        return [], []
    tasks = [asyncio.to_thread(_fetch_peer_registry_results, peer_url, query) for peer_url in peer_urls]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    merged: list[dict] = []
    peer_stats: list[dict] = []
    for peer_url, result in zip(peer_urls, responses):
        if isinstance(result, Exception):
            log_event("federation_discovery_error", f"Peer discovery failed for {peer_url}: {result}", hub_url=peer_url)
            continue
        if not isinstance(result, list):
            continue
        merged.extend(result)
        peer_stats.append({"hub_url": peer_url, "count": len(result)})
    return merged, peer_stats

def _provider_matches_requirement(provider_id: str, model_requirement: Optional[str]) -> bool:
    if not model_requirement:
        return True
    registry = db.get_registry(provider_id)
    if not registry:
        return True
    models = registry.get("models") or []
    skills = registry.get("skills") or []
    if not models and not skills:
        return True
    return model_requirement in models or model_requirement in skills

def _compute_provider_assignment_profile(provider_id: str, model_requirement: Optional[str]) -> dict:
    model_requirement_normalized = _normalize_model_requirement(model_requirement)
    registry = db.get_registry(provider_id) or {}
    reputation = db.get_reputation(provider_id) or {}
    models = [item.strip().lower() for item in registry.get("models", []) if isinstance(item, str)]
    skills = [item.strip().lower() for item in registry.get("skills", []) if isinstance(item, str)]
    availability = (registry.get("availability") or "unknown").strip().lower()
    capability_match = _provider_matches_requirement(provider_id, model_requirement_normalized)
    if not model_requirement_normalized:
        capability_score = 1.0
    elif model_requirement_normalized in models:
        capability_score = 1.0
    elif model_requirement_normalized in skills:
        capability_score = 0.8
    elif not models and not skills:
        capability_score = 0.5
    else:
        capability_score = 0.0
    availability_score_map = {
        "online": 1.0,
        "idle": 0.9,
        "busy": 0.4,
        "unknown": 0.3,
        "offline": 0.0
    }
    availability_score = availability_score_map.get(availability, 0.2)
    raw_score = float(reputation.get("score", 0.0) or 0.0)
    raw_score = max(0.0, min(5.0, raw_score))
    total_reviews = int(reputation.get("total_reviews", 0) or 0)
    confidence_base = max(1, ASSIGNMENT_REPUTATION_CONFIDENCE_REVIEWS)
    confidence = min(1.0, total_reviews / confidence_base)
    reputation_score = (raw_score / 5.0) * confidence + 0.5 * (1.0 - confidence)
    assignment_score = (
        ASSIGNMENT_REPUTATION_WEIGHT * reputation_score
        + ASSIGNMENT_AVAILABILITY_WEIGHT * availability_score
        + ASSIGNMENT_CAPABILITY_WEIGHT * capability_score
    )
    risk_reasons: list[str] = []
    if model_requirement_normalized and not capability_match:
        risk_reasons.append("capability_mismatch")
    if availability in RISK_REJECT_AVAILABILITY:
        risk_reasons.append(f"availability_{availability}")
    if total_reviews >= RISK_MIN_REPUTATION_REVIEWS and raw_score < RISK_MIN_REPUTATION_SCORE:
        risk_reasons.append("low_reputation")
    return {
        "provider_id": provider_id,
        "availability": availability,
        "reputation_score": raw_score,
        "total_reviews": total_reviews,
        "capability_match": capability_match,
        "assignment_score": assignment_score,
        "risk_reasons": risk_reasons
    }

def _select_rfc_recipients(consumer_id: str, model_requirement: Optional[str], nodes: list[tuple[str, WebSocket]]) -> list[tuple[str, WebSocket]]:
    selected: list[tuple[str, WebSocket, float]] = []
    for node_id, ws in nodes:
        if node_id == consumer_id:
            continue
        profile = _compute_provider_assignment_profile(node_id, model_requirement)
        if profile["risk_reasons"]:
            continue
        selected.append((node_id, ws, float(profile["assignment_score"])))
    selected.sort(key=lambda item: item[2], reverse=True)
    if RFC_TOP_K > 0:
        selected = selected[:RFC_TOP_K]
    return [(node_id, ws) for node_id, ws, _ in selected]

def _validate_timestamp(ts: str):
    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp")
    now = int(time.time())
    if abs(now - ts_int) > MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="Timestamp out of allowed window")

def _format_uptime(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def _get_system_uptime_seconds() -> Optional[float]:
    proc_uptime = "/proc/uptime"
    if os.path.exists(proc_uptime):
        try:
            with open(proc_uptime, "r", encoding="utf-8") as f:
                value = f.read().split()[0]
            return float(value)
        except Exception:
            return None
    try:
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return None

def _resolve_log_path(filename: str) -> Optional[str]:
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", filename),
        os.path.join(os.getcwd(), "logs", filename)
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

def _tail_lines(path: str, limit: int) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()[-limit:]

def _escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def _evict_completed_tasks_cache():
    now = time.time()
    removed = 0
    async with task_lock:
        if COMPLETED_TASK_CACHE_TTL_SECONDS > 0:
            expired = [
                task_id
                for task_id, data in completed_tasks.items()
                if now - float(data.get("completed_at", now)) > COMPLETED_TASK_CACHE_TTL_SECONDS
            ]
            for task_id in expired:
                del completed_tasks[task_id]
            removed += len(expired)
        if COMPLETED_TASK_CACHE_MAX_ITEMS > 0 and len(completed_tasks) > COMPLETED_TASK_CACHE_MAX_ITEMS:
            overflow = len(completed_tasks) - COMPLETED_TASK_CACHE_MAX_ITEMS
            oldest = sorted(
                completed_tasks.items(),
                key=lambda item: float(item[1].get("completed_at", 0.0))
            )[:overflow]
            for task_id, _ in oldest:
                del completed_tasks[task_id]
            removed += len(oldest)
    if removed > 0:
        log_event("completed_cache_evicted", f"Evicted {removed} completed task cache entries", removed=removed)

def _sweep_idempotency_records():
    if IDEMPOTENCY_TTL_SECONDS <= 0:
        return
    cutoff = time.time() - IDEMPOTENCY_TTL_SECONDS
    removed = db.delete_idempotency_before(cutoff)
    if removed > 0:
        log_event("idempotency_cleaned", f"Removed {removed} expired idempotency records", removed=removed)

async def _sweep_assigned_timeouts():
    if TASK_TIMEOUT_DEFAULT_SECONDS <= 0:
        return
    now = time.time()
    expired_tasks = db.get_assigned_tasks_for_timeout(
        now,
        TASK_TIMEOUT_DEFAULT_SECONDS,
        TASK_TIMEOUT_MIN_SECONDS,
        TASK_TIMEOUT_MAX_SECONDS,
    )
    if not expired_tasks:
        return
    for task in expired_tasks:
        if TIMEOUT_POLICY == "rebroadcast" and not task["target_node"]:
            if not db.requeue_task_if_assigned(task["task_id"], now):
                continue
            task_data = {
                "id": task["task_id"],
                "consumer_id": task["consumer_id"],
                "payload": task["payload"],
                "bounty": task["bounty"],
                "status": "bidding",
                "target_node": task["target_node"],
                "model_requirement": task["model_requirement"],
                "expires_in_seconds": task.get("expires_in_seconds"),
                "payload_uri": task.get("payload_uri"),
                "secret_data": task.get("result_payload")
            }
            async with task_lock:
                active_tasks[task["task_id"]] = task_data
            model_requirement = _normalize_model_requirement(task["model_requirement"])
            rfc_data = {
                "id": task["task_id"],
                "consumer_id": task["consumer_id"],
                "bounty": task["bounty"],
                "model_requirement": model_requirement,
                "payload_uri": task.get("payload_uri")
            }
            async with node_lock:
                broadcast_nodes = list(connected_nodes.items())
            candidate_nodes = _select_rfc_recipients(task["consumer_id"], model_requirement, broadcast_nodes)
            for node_id, ws in candidate_nodes:
                try:
                    await ws.send_json({"event": "rfc", "data": rfc_data})
                except Exception as exc:
                    log_event("broadcast_error", f"Failed to send RFC to {node_id}: {exc}", node_id=node_id, task_id=task["task_id"])
                    async with node_lock:
                        if connected_nodes.get(node_id) is ws:
                            del connected_nodes[node_id]
            log_event("task_requeued_timeout", f"Task {task['task_id'][:8]} requeued after timeout", task_id=task["task_id"], consumer_id=task["consumer_id"], bounty=task["bounty"])
            continue
        if not db.expire_task_if_assigned(task["task_id"], now):
            continue
        if task["bounty"] > 0:
            refunded = db.refund_escrow(task["task_id"], now)
            if refunded is None:
                db.add_balance(task["consumer_id"], task["bounty"])
            new_balance = db.get_balance(task["consumer_id"])
            log_audit("TIMEOUT_REFUND", task["consumer_id"], task["bounty"], new_balance, task["task_id"])
        async with task_lock:
            if task["task_id"] in active_tasks:
                del active_tasks[task["task_id"]]
        log_event("task_expired", f"Task {task['task_id'][:8]} expired after timeout", task_id=task["task_id"], consumer_id=task["consumer_id"], provider_id=task["provider_id"], bounty=task["bounty"])

async def _sweep_bidding_timeouts():
    """Expire bidding tasks that never got a provider and refund bounty"""
    if TASK_TIMEOUT_DEFAULT_SECONDS <= 0:
        return
    now = time.time()
    expired_bidding = db.get_bidding_tasks_for_timeout(
        now,
        TASK_TIMEOUT_DEFAULT_SECONDS,
        TASK_TIMEOUT_MIN_SECONDS,
        TASK_TIMEOUT_MAX_SECONDS,
    )
    if not expired_bidding:
        return
    for task in expired_bidding:
        if not db.expire_task_if_bidding(task["task_id"], now):
            continue
        if task.get("bounty", 0) > 0:
            refunded = db.refund_escrow(task["task_id"], now)
            if refunded is None:
                db.add_balance(task["consumer_id"], task["bounty"])
            new_balance = db.get_balance(task["consumer_id"])
            log_audit("BIDDING_TIMEOUT_REFUND", task["consumer_id"], task["bounty"], new_balance, task["task_id"])
        async with task_lock:
            if task["task_id"] in active_tasks:
                del active_tasks[task["task_id"]]
        log_event("task_expired_bidding", f"Task {task['task_id'][:8]} expired", task_id=task["task_id"], consumer_id=task["consumer_id"])

async def _assignment_timeout_worker():
    while True:
        try:
            await _sweep_assigned_timeouts()
            await _sweep_bidding_timeouts()
        except Exception as exc:
            log_event("timeout_sweep_failed", f"Timeout sweep failed: {exc}")
        await asyncio.sleep(ASSIGNMENT_SWEEP_INTERVAL_SECONDS)

async def _maintenance_worker():
    while True:
        try:
            await _evict_completed_tasks_cache()
            _sweep_idempotency_records()
            await _sweep_node_activity()
            await _reconcile_live_registry_presence()
        except Exception as exc:
            log_event("maintenance_sweep_failed", f"Maintenance sweep failed: {exc}")
        await asyncio.sleep(max(1, MAINTENANCE_SWEEP_INTERVAL_SECONDS))

@app.on_event("startup")
async def start_timeout_worker():
    await _load_active_tasks_from_db()
    await _reconcile_live_registry_presence()
    asyncio.create_task(_assignment_timeout_worker())
    asyncio.create_task(_maintenance_worker())
    if REQUIRE_TLS:
        log_event("transport_policy", "TLS enforcement enabled", require_tls=True, trust_proxy_proto=TRUST_PROXY_PROTO)
    else:
        log_event("transport_policy_warning", "TLS enforcement disabled", require_tls=False, trust_proxy_proto=TRUST_PROXY_PROTO)


@app.on_event("shutdown")
async def shutdown_hub():
    async with node_lock:
        sockets = list(connected_nodes.values())
        connected_nodes.clear()
    for socket in sockets:
        try:
            await socket.close(code=1001, reason="Hub shutting down")
        except Exception:
            pass

def _read_recent_events(limit: int) -> list[dict]:
    path = _resolve_log_path("hub.json")
    if not path:
        return []
    lines = _tail_lines(path, limit)
    events = []
    for line in lines:
        try:
            entry = json.loads(line)
            events.append({
                "timestamp": entry.get("timestamp", ""),
                "event": entry.get("event", ""),
                "message": entry.get("message", "")
            })
        except Exception:
            continue
    return events

def _read_audit_entries_for_node(node_id: str, limit: int) -> list[str]:
    path = _resolve_log_path("ledger_audit.log")
    if not path:
        return []
    scan_limit = min(max(limit * 50, 200), 5000)
    lines = _tail_lines(path, scan_limit)
    needle = f"Node: {node_id} |"
    matches = [line.strip() for line in reversed(lines) if needle in line]
    return list(reversed(matches[:limit]))

async def verify_request(
    request: Request,
    x_mep_nodeid: str = Header(...),
    x_mep_timestamp: str = Header(...),
    x_mep_signature: str = Header(...)
) -> str:
    client_host = _request_client_ip(request)
    if not _is_allowed_ip(client_host):
        raise HTTPException(status_code=403, detail="Client IP not allowed")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    _apply_rate_limit(f"{x_mep_nodeid}:{client_host or 'unknown'}:{request.url.path}")
    _validate_timestamp(x_mep_timestamp)

    payload_str = body.decode('utf-8')

    pub_pem = db.get_pub_pem(x_mep_nodeid)
    if not pub_pem:
        raise HTTPException(status_code=401, detail="Unknown Node ID. Please register first.")

    if not auth.verify_signature(pub_pem, payload_str, x_mep_timestamp, x_mep_signature):
        raise HTTPException(status_code=401, detail="Invalid cryptographic signature.")

    return x_mep_nodeid

@app.post("/register")
async def register_node(node: NodeRegistration, request: Request):
    client_host = _request_client_ip(request)
    if not _is_allowed_ip(client_host):
        raise HTTPException(status_code=403, detail="Client IP not allowed")
    _apply_rate_limit(f"{client_host or 'unknown'}:/register")
    validated_pubkey = _validate_registration_pubkey(node.pubkey)
    # Registration derives the Node ID from the validated Ed25519 public key PEM
    node_id = auth.derive_node_id(validated_pubkey)
    balance = db.register_node(node_id, validated_pubkey)
    if node.alias or getattr(node, 'x25519_public_key', None):
        db.upsert_registry(node_id, node.alias, [], [], {}, "offline", time.time(), getattr(node, 'x25519_public_key', None))

    log_event("node_registered", f"Node {node_id} registered with starting balance {balance}", node_id=node_id, starting_balance=balance)
    log_audit("REGISTER", node_id, balance, balance, "START_BONUS")

    hub_url, ws_url = get_hub_urls(request)

    return {"status": "success", "node_id": node_id, "balance": balance, "hub_url": hub_url, "ws_url": ws_url}

@app.post("/registry/update")
async def update_registry(payload: RegistryUpdate, authenticated_node: str = Depends(verify_request)):
    skills = [item.strip().lower() for item in payload.skills or [] if item and item.strip()]
    models = [item.strip().lower() for item in payload.models or [] if item and item.strip()]
    metadata = payload.metadata or {}
    availability = _normalize_availability(payload.availability)
    if availability is None:
        existing = db.get_registry(authenticated_node)
        availability = existing.get("availability") if existing else "unknown"
    availability = await _coerce_live_availability(authenticated_node, availability)
    db.upsert_registry(authenticated_node, payload.alias, skills, models, metadata, availability, time.time())
    return {"status": "success", "node_id": authenticated_node}

@app.post("/registry/availability")
async def update_availability(payload: AvailabilityUpdate, authenticated_node: str = Depends(verify_request)):
    availability = _normalize_availability(payload.availability)
    availability = await _coerce_live_availability(authenticated_node, availability)
    db.update_registry_availability(authenticated_node, availability, time.time())
    await _track_node_activity(authenticated_node)
    return {"status": "success", "node_id": authenticated_node, "availability": availability}

@app.post("/registry/heartbeat")
async def registry_heartbeat(payload: RegistryHeartbeat, request: Request, authenticated_node: str = Depends(verify_request)):
    availability = _normalize_availability(payload.availability)
    if availability is None:
        existing = db.get_registry(authenticated_node)
        availability = existing.get("availability") if existing else "unknown"
    availability = await _coerce_live_availability(authenticated_node, availability)
    db.update_registry_availability(authenticated_node, availability, time.time())
    await _track_node_activity(authenticated_node)
    hub_url, ws_url = get_hub_urls(request)
    return {"status": "success", "node_id": authenticated_node, "availability": availability, "hub_url": hub_url, "ws_url": ws_url}

@app.get("/registry/search")
async def search_registry(
    alias: Optional[str] = None,
    skill: Optional[str] = None,
    model: Optional[str] = None,
    availability: Optional[str] = None,
    min_score: Optional[float] = None,
    min_reviews: Optional[int] = None,
    max_age_minutes: Optional[float] = None,
    limit: int = 20
):
    query = _build_registry_query(alias, skill, model, availability, min_score, min_reviews, max_age_minutes, limit)
    results = _search_registry_local(query)
    return {"count": len(results), "results": results}


@app.post("/mesh/assemble")
async def mesh_assemble(payload: MeshAssembleRequest, authenticated_node: str = Depends(verify_request)):
    trigger = _normalize_mesh_trigger(payload.trigger)
    timeout_seconds = _normalize_mesh_timeout_seconds(payload.timeout_seconds)
    now = time.time()
    await _purge_expired_mesh_assemblies(now)
    candidates = await _collect_mesh_candidates()
    roles, missing_roles = _assign_mesh_roles(candidates)
    complete = len(missing_roles) == 0
    assembly_id = str(uuid.uuid4())
    record = {
        "assembly_id": assembly_id,
        "trigger": trigger,
        "requested_by": authenticated_node,
        "created_at": now,
        "expires_at": now + timeout_seconds,
        "timeout_seconds": timeout_seconds,
        "roles": roles,
        "missing_roles": missing_roles,
    }
    async with mesh_lock:
        mesh_assemblies[assembly_id] = record
    result = {
        "assembly_id": assembly_id,
        "roles": roles,
        "complete": complete,
    }
    if missing_roles:
        result["degraded_warning"] = f"No suitable node for role(s): {', '.join(missing_roles)}"
    log_event(
        "mesh_assembled",
        f"Mesh assembly {assembly_id[:8]} created by {authenticated_node}",
        assembly_id=assembly_id,
        trigger=trigger,
        requester=authenticated_node,
        complete=complete,
        missing_roles=missing_roles,
    )
    return result


@app.get("/mesh/status")
async def mesh_status(assembly_id: str, authenticated_node: str = Depends(verify_request)):
    del authenticated_node  # authenticated access only; no per-node ownership restrictions yet.
    now = time.time()
    await _purge_expired_mesh_assemblies(now)
    async with mesh_lock:
        assembly = mesh_assemblies.get(assembly_id)
    if not assembly:
        raise HTTPException(status_code=404, detail="Assembly not found or expired")

    candidates = await _collect_mesh_candidates()
    current_by_id = {item["node_id"]: item for item in candidates}
    dropped_roles: dict[str, dict] = {}
    active_roles: dict[str, dict] = {}

    for role, assigned in assembly.get("roles", {}).items():
        node_id = assigned.get("node_id")
        current = current_by_id.get(node_id or "")
        if not current:
            dropped_roles[role] = {
                "node_id": node_id,
                "reason": "node_unavailable",
            }
            continue
        active_roles[role] = _mesh_role_payload(current)

    missing_roles = list(assembly.get("missing_roles", []))
    target_roles: list[str] = []
    for role in ("strategist", "implementer", "facilitator", "scout"):
        if role in dropped_roles or role in missing_roles:
            target_roles.append(role)

    used = {item["node_id"] for item in active_roles.values() if item.get("node_id")}
    suggestions: dict[str, dict] = {}
    for role in target_roles:
        candidate = _pick_mesh_candidate(role, candidates, used)
        if not candidate:
            continue
        used.add(candidate["node_id"])
        suggestions[role] = _mesh_role_payload(candidate)

    complete = len(dropped_roles) == 0 and len(missing_roles) == 0
    response = {
        "assembly_id": assembly_id,
        "trigger": assembly.get("trigger"),
        "created_at": assembly.get("created_at"),
        "expires_at": assembly.get("expires_at"),
        "complete": complete,
        "roles": active_roles,
        "dropped_roles": dropped_roles,
        "reassignment_suggestions": suggestions,
    }
    if not complete:
        unresolved = [role for role in target_roles if role not in suggestions]
        if unresolved:
            response["degraded_warning"] = f"Unfilled role(s): {', '.join(unresolved)}"
    return response


@app.post("/brainstorm/sessions/create")
async def brainstorm_create(
    payload: BrainstormSessionCreate,
    authenticated_node: str = Depends(verify_request),
):
    if authenticated_node != payload.owner_id:
        raise HTTPException(status_code=403, detail="Cannot create session for another node")
    participants = _normalize_brainstorm_participants(payload.owner_id, payload.participants)
    if len(participants) < 2:
        raise HTTPException(status_code=400, detail="Brainstorm session requires at least 2 participants")
    now = time.time()
    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "owner_id": payload.owner_id,
        "participants": participants,
        "topic": (payload.topic or "").strip(),
        "created_at": now,
        "updated_at": now,
        "status": "active",
        "max_messages": int(payload.max_messages or 200),
        "messages": [],
        "message_seq": 0,
    }
    async with brainstorm_lock:
        brainstorm_sessions[session_id] = session
    log_event(
        "brainstorm_session_created",
        f"Brainstorm session {session_id[:8]} created by {payload.owner_id}",
        session_id=session_id,
        owner_id=payload.owner_id,
        participant_count=len(participants),
    )
    return {
        "status": "success",
        "session_id": session_id,
        "owner_id": payload.owner_id,
        "participants": participants,
        "topic": session["topic"],
    }


@app.post("/brainstorm/sessions/post")
async def brainstorm_post(
    payload: BrainstormSessionPost,
    authenticated_node: str = Depends(verify_request),
):
    async with brainstorm_lock:
        session = brainstorm_sessions.get(payload.session_id)
        if not session or session.get("status") != "active":
            raise HTTPException(status_code=404, detail="Brainstorm session not found or inactive")
        participants = list(session.get("participants", []))
        if authenticated_node not in participants:
            raise HTTPException(status_code=403, detail="Node is not a participant of this session")
        max_messages = int(session.get("max_messages", 200))
        if len(session["messages"]) >= max_messages:
            raise HTTPException(status_code=400, detail="Brainstorm session reached max messages")
        message_id = f"{payload.session_id}:{int(session.get('message_seq', 0)) + 1}"
        message = {
            "message_id": message_id,
            "session_id": payload.session_id,
            "sender_id": authenticated_node,
            "content": payload.message.strip(),
            "reply_to_message_id": payload.reply_to_message_id,
            "created_at": time.time(),
        }
        session["message_seq"] = int(session.get("message_seq", 0)) + 1
        session["messages"].append(message)
        session["updated_at"] = message["created_at"]

    fanout_payload = {
        "event": "brainstorm_message",
        "data": {
            "session_id": payload.session_id,
            "topic": session.get("topic"),
            "message": message,
            "participants": participants,
        },
    }
    delivered_to: list[str] = []
    async with node_lock:
        participant_sockets = [(node_id, connected_nodes.get(node_id)) for node_id in participants]
    for node_id, ws in participant_sockets:
        if not ws:
            continue
        try:
            await ws.send_json(fanout_payload)
            delivered_to.append(node_id)
        except Exception as exc:
            log_event(
                "brainstorm_fanout_failed",
                f"Failed fanout for session {payload.session_id[:8]} to {node_id}: {exc}",
                session_id=payload.session_id,
                node_id=node_id,
            )
            async with node_lock:
                if connected_nodes.get(node_id) is ws:
                    del connected_nodes[node_id]
    return {"status": "success", "message_id": message_id, "delivered_to": delivered_to}


@app.get("/brainstorm/sessions/{session_id}")
async def brainstorm_get_session(
    session_id: str,
    limit: int = 100,
    authenticated_node: str = Depends(verify_request),
):
    safe_limit = max(1, min(limit, 500))
    async with brainstorm_lock:
        session = brainstorm_sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Brainstorm session not found")
        if authenticated_node not in session.get("participants", []):
            raise HTTPException(status_code=403, detail="Node is not a participant of this session")
        messages = list(session.get("messages", []))
        payload = {
            "session_id": session["session_id"],
            "owner_id": session["owner_id"],
            "participants": list(session.get("participants", [])),
            "topic": session.get("topic"),
            "status": session.get("status"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "message_count": len(messages),
            "messages": messages[-safe_limit:],
        }
    return payload


@app.get("/brainstorm/sessions")
async def brainstorm_list_sessions(authenticated_node: str = Depends(verify_request)):
    now = time.time()
    async with brainstorm_lock:
        sessions = [
            {
                "session_id": session["session_id"],
                "owner_id": session["owner_id"],
                "participants": list(session.get("participants", [])),
                "topic": session.get("topic"),
                "status": session.get("status"),
                "updated_at": session.get("updated_at"),
                "message_count": len(session.get("messages", [])),
            }
            for session in brainstorm_sessions.values()
            if authenticated_node in session.get("participants", [])
        ]
    sessions.sort(key=lambda item: float(item.get("updated_at") or now), reverse=True)
    return {"count": len(sessions), "sessions": sessions}

@app.get("/federation/peers")
async def get_federation_peers():
    peers = await _list_federation_peers()
    return {"enabled": FEDERATION_ENABLED, "hub_id": HUB_ID, "count": len(peers), "peers": peers}

@app.post("/federation/peers")
async def add_federation_peer(payload: FederationPeerUpsert, x_mep_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_mep_admin_key)
    normalized_hub_url = _normalize_hub_url(payload.hub_url)
    async with federation_peer_lock:
        dynamic_federation_peers.add(normalized_hub_url)
    peers = await _list_federation_peers()
    return {"status": "success", "hub_url": normalized_hub_url, "count": len(peers), "peers": peers}

@app.delete("/federation/peers")
async def remove_federation_peer(hub_url: str, x_mep_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_mep_admin_key)
    normalized_hub_url = _normalize_hub_url(hub_url)
    async with federation_peer_lock:
        removed = normalized_hub_url in dynamic_federation_peers
        if removed:
            dynamic_federation_peers.remove(normalized_hub_url)
    peers = await _list_federation_peers()
    return {"status": "success", "hub_url": normalized_hub_url, "removed": removed, "count": len(peers), "peers": peers}

@app.get("/federation/discovery")
async def federation_discovery(
    alias: Optional[str] = None,
    skill: Optional[str] = None,
    model: Optional[str] = None,
    availability: Optional[str] = None,
    min_score: Optional[float] = None,
    min_reviews: Optional[int] = None,
    max_age_minutes: Optional[float] = None,
    limit: int = 20,
    include_local: bool = True
):
    query = _build_registry_query(alias, skill, model, availability, min_score, min_reviews, max_age_minutes, min(limit, FEDERATION_REMOTE_LIMIT))
    local_results = _search_registry_local(query) if include_local else []
    for item in local_results:
        item["source_hub"] = HUB_ID
        item["source_hub_url"] = None
    remote_results, peer_stats = await _discover_remote_registry(query)
    merged: list[dict] = []
    seen_node_ids: set[str] = set()
    for item in local_results + remote_results:
        node_id = str(item.get("node_id", "")).strip()
        if not node_id or node_id in seen_node_ids:
            continue
        merged.append(item)
        seen_node_ids.add(node_id)
        if len(merged) >= max(1, min(limit, 100)):
            break
    return {
        "status": "success",
        "hub_id": HUB_ID,
        "federation_enabled": FEDERATION_ENABLED,
        "count": len(merged),
        "local_count": len(local_results),
        "remote_count": len(remote_results),
        "peer_stats": peer_stats,
        "results": merged
    }

@app.get("/registry/{node_id}")
async def get_registry(node_id: str):
    entry = db.get_registry(node_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Registry entry not found")
    return entry

@app.post("/reputation/submit")
async def submit_reputation(payload: ReputationSubmit, authenticated_node: str = Depends(verify_request)):
    if payload.rating < 1 or payload.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")
    task = db.get_task(payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail="Task not completed")
    if task["consumer_id"] != authenticated_node:
        raise HTTPException(status_code=403, detail="Only the consumer can submit a review")
    if task.get("provider_id") != payload.provider_id:
        raise HTTPException(status_code=400, detail="Provider does not match task")
    result = db.submit_review(payload.task_id, authenticated_node, payload.provider_id, payload.rating, time.time())
    if result["status"] == "exists":
        raise HTTPException(status_code=409, detail="Review already submitted for this task")
    return {"status": "success", "provider_id": payload.provider_id, "score": result["score"], "total_reviews": result["total_reviews"]}

@app.get("/reputation/{node_id}")
async def get_reputation(node_id: str):
    entry = db.get_reputation(node_id)
    if not entry:
        return {"node_id": node_id, "score": 0.0, "total_reviews": 0}
    return entry

@app.get("/balance/{node_id}")
async def get_balance(node_id: str):
    balance = db.get_balance(node_id)
    if balance is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return {"node_id": node_id, "balance_seconds": balance}

@app.post("/tasks/submit")
async def submit_task(
    task: TaskCreate,
    authenticated_node: str = Depends(verify_request),
    x_mep_idempotency_key: Optional[str] = Header(default=None)
):
    consumer_id = _task_consumer_id(task)
    if authenticated_node != consumer_id:
        raise HTTPException(status_code=403, detail="Cannot submit tasks on behalf of another node")

    normalized_payload_uri = _normalize_artifact_uri(task.payload_uri, "payload_uri")
    payload = _task_instructions(task)
    if not payload and not normalized_payload_uri:
        raise HTTPException(status_code=400, detail="Task requires payload or payload_uri")
    if len(payload) > MAX_PAYLOAD_CHARS:
        raise HTTPException(status_code=413, detail="Task payload too large")
    target_node = _task_target_node(task)
    target_capability = _task_target_capability(task)
    economics = _task_economics(task)
    bounty = float(economics["bounty_seconds"])
    _validate_interbot_payload_if_enabled(consumer_id, economics, target_node, payload)
    if target_node and bounty == 0.0:
        await _anti_loop_check_and_record(consumer_id, target_node, payload)
    
    if x_mep_idempotency_key:
        existing = db.get_idempotency(authenticated_node, "/tasks/submit", x_mep_idempotency_key)
        if existing:
            return existing["response"]

    consumer_balance = db.get_balance(consumer_id)
    if consumer_balance is None:
        raise HTTPException(status_code=404, detail="Consumer node not found")

    # If bounty is positive, consumer is PAYING. Check consumer balance.
    if bounty > 0 and consumer_balance < bounty:
        raise HTTPException(status_code=400, detail="Insufficient SECONDS balance to pay for task")
    if bounty < 0 and not task.secret_data:
        raise HTTPException(status_code=400, detail="Data market tasks (bounty < 0) require secret_data")
    if task.expires_in_seconds is not None:
        task.expires_in_seconds = max(
            TASK_TIMEOUT_MIN_SECONDS,
            min(int(task.expires_in_seconds), TASK_TIMEOUT_MAX_SECONDS),
        )

    task_id = str(uuid.uuid4())
    now = time.time()

    # Note: If bounty is negative, consumer is SELLING data. We don't deduct here.
    # We will deduct from the provider when they complete the task.
    if bounty > 0:
        success = db.deduct_balance(consumer_id, bounty)
        if not success:
            log_event("task_rejected", f"Node {consumer_id} lacks SECONDS to submit task", consumer_id=consumer_id, bounty=bounty)
            raise HTTPException(status_code=400, detail="Insufficient SECONDS balance")
        db.create_escrow(task_id, consumer_id, bounty, now)
        new_balance = db.get_balance(consumer_id)
        log_audit("ESCROW", consumer_id, -bounty, new_balance, task_id)

    log_event("task_submitted", f"Task {task_id[:8]} broadcasted by {consumer_id} for {bounty}", consumer_id=consumer_id, task_id=task_id, bounty=bounty)

    task_data = {
        "id": task_id,
        "consumer_id": consumer_id,
        "payload": payload,
        "bounty": bounty,
        "status": "bidding",
        "source": task.source or {"node_id": consumer_id},
        "intent": task.intent or {"type": "analysis.request"},
        "task": {
            "instructions": payload,
            "expected_output": _task_expected_output(task),
        },
        "economics": economics,
        "target_node": target_node,
        "model_requirement": target_capability,
        "expires_in_seconds": task.expires_in_seconds,
        "payload_uri": normalized_payload_uri,
        "secret_data": task.secret_data
    }
    db.create_task(
        task_id,
        consumer_id,
        payload,
        bounty,
        "bidding",
        target_node,
        target_capability,
        now,
        result_payload=task.secret_data,
        payload_uri=normalized_payload_uri,
        expires_in_seconds=task.expires_in_seconds,
    )
    async with task_lock:
        active_tasks[task_id] = task_data

    # Target specific node if requested (Direct Message skips bidding)
    if target_node:
        async with node_lock:
            target_ws = connected_nodes.get(target_node)
        if target_ws:
            try:
                async with task_lock:
                    task_data["status"] = "assigned"
                    task_data["provider_id"] = target_node
                db.update_task_assignment(task_id, target_node, "assigned", time.time())
                await target_ws.send_json({"event": "new_task", "data": task_data})
                response_payload = {"status": "success", "task_id": task_id, "routed_to": target_node}
                if x_mep_idempotency_key:
                    db.set_idempotency(authenticated_node, "/tasks/submit", x_mep_idempotency_key, response_payload, 200, time.time())
                return response_payload
            except Exception as exc:
                log_event("direct_message_failed", f"Failed to route {task_id[:8]} to {target_node}: {exc}", task_id=task_id, provider_id=target_node)
                async with task_lock:
                    task_data["status"] = "bidding"
                    if "provider_id" in task_data:
                        del task_data["provider_id"]
                db.update_task_status(task_id, "bidding", time.time())
                async with node_lock:
                    if connected_nodes.get(target_node) is target_ws:
                        del connected_nodes[target_node]
                return {"status": "error", "detail": "Target node disconnected"}
        return {"status": "error", "detail": "Target node not currently connected to Hub"}

    rfc_data = {
        "id": task_id,
        "consumer_id": consumer_id,
        "bounty": bounty,
        "model_requirement": target_capability,
        "payload_uri": normalized_payload_uri
    }
    async with node_lock:
        broadcast_nodes = list(connected_nodes.items())
    candidate_nodes = _select_rfc_recipients(consumer_id, target_capability, broadcast_nodes)
    for node_id, ws in candidate_nodes:
        try:
            await ws.send_json({"event": "rfc", "data": rfc_data})
        except Exception as exc:
            log_event("broadcast_error", f"Failed to send RFC to {node_id}: {exc}", node_id=node_id, task_id=task_id)
            async with node_lock:
                if connected_nodes.get(node_id) is ws:
                    del connected_nodes[node_id]

    response_payload = {"status": "success", "task_id": task_id}
    if not candidate_nodes and FEDERATION_ENABLED:
        discovery_query = _build_registry_query(
            alias=None,
            skill=None,
            model=target_capability,
            availability="online",
            min_score=None,
            min_reviews=None,
            max_age_minutes=DEFAULT_REGISTRY_MAX_AGE_MINUTES if DEFAULT_REGISTRY_MAX_AGE_MINUTES > 0 else None,
            limit=min(20, FEDERATION_REMOTE_LIMIT)
        )
        _, peer_stats = await _discover_remote_registry(discovery_query)
        routed_hubs = [item for item in peer_stats if item["count"] > 0]
        if routed_hubs:
            response_payload["federation_hints"] = routed_hubs
    if x_mep_idempotency_key:
        db.set_idempotency(authenticated_node, "/tasks/submit", x_mep_idempotency_key, response_payload, 200, time.time())
    return response_payload

@app.post("/tasks/cancel")
async def cancel_task(
    cancel: TaskCancel,
    authenticated_node: str = Depends(verify_request),
    x_mep_idempotency_key: Optional[str] = Header(default=None)
):
    if x_mep_idempotency_key:
        existing = db.get_idempotency(authenticated_node, "/tasks/cancel", x_mep_idempotency_key)
        if existing:
            return existing["response"]

    async with task_lock:
        task = active_tasks.get(cancel.task_id)
    if not task:
        db_task = db.get_task(cancel.task_id)
        if not db_task:
            raise HTTPException(status_code=404, detail="Task not found")
        task = {
            "id": db_task["task_id"],
            "consumer_id": db_task["consumer_id"],
            "payload": db_task["payload"],
            "bounty": db_task["bounty"],
            "status": db_task["status"],
            "target_node": db_task["target_node"],
            "model_requirement": db_task["model_requirement"],
            "expires_in_seconds": db_task.get("expires_in_seconds"),
            "provider_id": db_task["provider_id"],
            "payload_uri": db_task.get("payload_uri"),
            "secret_data": db_task.get("result_payload")
        }

    if authenticated_node != task["consumer_id"]:
        raise HTTPException(status_code=403, detail="Cannot cancel tasks on behalf of another node")

    now = time.time()
    if not db.cancel_task_if_open(cancel.task_id, now):
        raise HTTPException(status_code=400, detail="Task cannot be cancelled at this stage")

    if task["bounty"] > 0:
        refunded = db.refund_escrow(cancel.task_id, now)
        if refunded is None:
            db.add_balance(task["consumer_id"], task["bounty"])
        new_balance = db.get_balance(task["consumer_id"])
        log_audit("REFUND", task["consumer_id"], task["bounty"], new_balance, cancel.task_id)

    async with task_lock:
        if cancel.task_id in active_tasks:
            del active_tasks[cancel.task_id]

    log_event("task_cancelled", f"Task {cancel.task_id[:8]} cancelled by {task['consumer_id']}", consumer_id=task["consumer_id"], task_id=cancel.task_id, bounty=task["bounty"])

    response_payload = {"status": "success", "task_id": cancel.task_id, "state": "cancelled"}
    if x_mep_idempotency_key:
        db.set_idempotency(authenticated_node, "/tasks/cancel", x_mep_idempotency_key, response_payload, 200, time.time())
    return response_payload

@app.post("/tasks/bid")
async def place_bid(bid: TaskBid, authenticated_node: str = Depends(verify_request)):
    if authenticated_node != bid.provider_id:
        raise HTTPException(status_code=403, detail="Cannot bid on behalf of another node")

    async with task_lock:
        task = active_tasks.get(bid.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found or already completed")
        if task["status"] != "bidding":
            return {"status": "rejected", "detail": "Task already assigned to another node"}
        model_requirement = _normalize_model_requirement(task.get("model_requirement"))
        assignment_profile = _compute_provider_assignment_profile(bid.provider_id, model_requirement)
        if assignment_profile["risk_reasons"]:
            risk_reasons = ", ".join(assignment_profile["risk_reasons"])
            return {"status": "rejected", "detail": f"Provider rejected by risk control: {risk_reasons}"}
        if not db.assign_task_if_open(bid.task_id, bid.provider_id, time.time()):
            return {"status": "rejected", "detail": "Task already assigned to another node"}
        task["status"] = "assigned"
        task["provider_id"] = bid.provider_id
        payload = task["payload"]
        consumer_id = task["consumer_id"]
        model_requirement = task.get("model_requirement")
        payload_uri = task.get("payload_uri")
        bounty = task["bounty"]

    log_event("bid_accepted", f"Task {bid.task_id[:8]} assigned to {bid.provider_id}", task_id=bid.task_id, provider_id=bid.provider_id, bounty=bounty)

    return {
        "status": "accepted",
        "payload": payload,
        "consumer_id": consumer_id,
        "model_requirement": model_requirement,
        "payload_uri": payload_uri,
        "secret_data": task.get("secret_data", task.get("result_payload")),
        "assignment_score": assignment_profile["assignment_score"]
    }

@app.post("/tasks/complete")
async def complete_task(
    result: TaskResult,
    authenticated_node: str = Depends(verify_request),
    x_mep_idempotency_key: Optional[str] = Header(default=None)
):
    if authenticated_node != result.provider_id:
        raise HTTPException(status_code=403, detail="Cannot complete tasks on behalf of another node")

    normalized_result_uri = _normalize_artifact_uri(result.result_uri, "result_uri")
    result_payload = result.result_payload or ""
    if not result_payload and not normalized_result_uri:
        raise HTTPException(status_code=400, detail="Task result requires result_payload or result_uri")
    if len(result_payload) > MAX_PAYLOAD_CHARS:
        raise HTTPException(status_code=413, detail="Result payload too large")
    
    if x_mep_idempotency_key:
        existing = db.get_idempotency(authenticated_node, "/tasks/complete", x_mep_idempotency_key)
        if existing:
            return existing["response"]

    async with task_lock:
        task = active_tasks.get(result.task_id)
    if not task:
        db_task = db.get_task(result.task_id)
        if not db_task or db_task["status"] not in ("bidding", "assigned"):
            raise HTTPException(status_code=404, detail="Task not found or already claimed")
        task = {
            "id": db_task["task_id"],
            "consumer_id": db_task["consumer_id"],
            "payload": db_task["payload"],
            "bounty": db_task["bounty"],
            "status": db_task["status"],
            "target_node": db_task["target_node"],
            "model_requirement": db_task["model_requirement"],
            "provider_id": db_task["provider_id"],
            "payload_uri": db_task.get("payload_uri"),
            "secret_data": db_task.get("result_payload")
        }
        async with task_lock:
            active_tasks[result.task_id] = task

    assigned_provider = task.get("provider_id")
    if assigned_provider and assigned_provider != result.provider_id:
        raise HTTPException(status_code=409, detail="Task assigned to another provider")

    provider_balance = db.get_balance(result.provider_id)
    if provider_balance is None:
        db.set_balance(result.provider_id, 0.0)

    # Transfer SECONDS based on positive or negative bounty
    bounty = task["bounty"]
    if bounty >= 0:
        released = db.release_escrow(result.task_id, result.provider_id, time.time())
        if released is None:
            db.add_balance(result.provider_id, bounty)
        new_balance = db.get_balance(result.provider_id)
        log_audit("ESCROW_RELEASE", result.provider_id, bounty, new_balance, result.task_id)
    else:
        # Data Market: Provider PAYS to receive this payload/task
        cost = abs(bounty)
        success = db.deduct_balance(result.provider_id, cost)
        if not success:
            log_event("data_purchase_failed", f"Provider {result.provider_id} lacks SECONDS to buy {result.task_id}", task_id=result.task_id, provider_id=result.provider_id, cost=cost)
            raise HTTPException(status_code=400, detail="Provider lacks SECONDS to buy this data")

        p_balance = db.get_balance(result.provider_id) or 0.0
        log_audit("BUY_DATA", result.provider_id, -cost, p_balance, result.task_id)

        db.add_balance(task["consumer_id"], cost) # The sender earns SECONDS
        c_balance = db.get_balance(task["consumer_id"]) or 0.0
        log_audit("SELL_DATA", task["consumer_id"], cost, c_balance, result.task_id)

    log_event("task_completed", f"Task {result.task_id[:8]} completed by {result.provider_id}", task_id=result.task_id, provider_id=result.provider_id, bounty=bounty)

    # Move task to completed
    async with task_lock:
        task["status"] = "completed"
        task["provider_id"] = result.provider_id
        task["result"] = result_payload
        task["completed_at"] = time.time()
        completed_tasks[result.task_id] = task
        if result.task_id in active_tasks:
            del active_tasks[result.task_id]
    db.update_task_result(result.task_id, result.provider_id, result_payload, "completed", time.time(), result_uri=normalized_result_uri)
    await _evict_completed_tasks_cache()

    # ROUTE RESULT BACK TO CONSUMER VIA WEBSOCKET
    consumer_id = task["consumer_id"]
    async with node_lock:
        consumer_ws = connected_nodes.get(consumer_id)
    if consumer_ws:
        try:
            await consumer_ws.send_json({
                "event": "task_result",
                "data": {
                    "task_id": result.task_id,
                    "provider_id": result.provider_id,
                    "result_payload": result_payload,
                    "result_uri": normalized_result_uri,
                    "bounty_spent": task["bounty"]
                }
            })
        except Exception as exc:
            log_event("deliver_result_failed", f"Failed to deliver result {result.task_id[:8]} to {consumer_id}: {exc}", task_id=result.task_id, consumer_id=consumer_id)
            async with node_lock:
                if connected_nodes.get(consumer_id) is consumer_ws:
                    del connected_nodes[consumer_id]

    response_payload = {"status": "success", "earned": task["bounty"], "new_balance": db.get_balance(result.provider_id)}
    if x_mep_idempotency_key:
        db.set_idempotency(authenticated_node, "/tasks/complete", x_mep_idempotency_key, response_payload, 200, time.time())
    return response_payload

@app.get("/tasks/result/{task_id}")
async def get_task_result(task_id: str, authenticated_node: str = Depends(verify_request)):
    task = db.get_task(task_id)
    if not task or task["status"] != "completed":
        raise HTTPException(status_code=404, detail="Task not found or not completed")
    if authenticated_node not in (task["consumer_id"], task["provider_id"]):
        raise HTTPException(status_code=403, detail="Not authorized to view this result")
    return {
        "task_id": task["task_id"],
        "consumer_id": task["consumer_id"],
        "provider_id": task["provider_id"],
        "bounty": task["bounty"],
        "result_payload": task["result_payload"],
        "result_uri": task.get("result_uri")
    }

@app.post("/disputes/open")
async def open_dispute(payload: DisputeOpen, authenticated_node: str = Depends(verify_request)):
    task = db.get_task(payload.task_id)
    if not task or task["status"] != "completed":
        raise HTTPException(status_code=404, detail="Task not found or not completed")
    if task["consumer_id"] != authenticated_node:
        raise HTTPException(status_code=403, detail="Only the consumer can open a dispute")
    if float(task.get("bounty", 0.0) or 0.0) <= 0:
        raise HTTPException(status_code=400, detail="Disputes require escrow-backed positive bounty tasks")
    escrow = db.get_escrow(payload.task_id)
    if not escrow or escrow.get("status") not in ("released", "held"):
        raise HTTPException(status_code=400, detail="Escrow not eligible for dispute")
    if task["updated_at"] and (time.time() - float(task["updated_at"])) > DISPUTE_WINDOW_SECONDS:
        raise HTTPException(status_code=400, detail="Dispute window expired")
    reason = _normalize_dispute_reason(payload.reason)
    dispute_id = db.open_dispute(payload.task_id, task["consumer_id"], task["provider_id"], reason, time.time())
    if dispute_id == "exists":
        raise HTTPException(status_code=409, detail="Dispute already exists")
    log_event("dispute_opened", f"Dispute opened for task {payload.task_id[:8]}", task_id=payload.task_id, consumer_id=task["consumer_id"], provider_id=task["provider_id"])
    return {
        "status": "success",
        "dispute_id": dispute_id,
        "task_id": payload.task_id,
        "escrow_status": escrow.get("status"),
        "reason": reason
    }

@app.get("/disputes/{task_id}")
async def get_dispute(task_id: str, authenticated_node: str = Depends(verify_request)):
    dispute = db.get_dispute(task_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if authenticated_node not in (dispute["consumer_id"], dispute["provider_id"]):
        raise HTTPException(status_code=403, detail="Not authorized to view this dispute")
    escrow = db.get_escrow(task_id)
    return {
        "dispute_id": dispute["dispute_id"],
        "task_id": dispute["task_id"],
        "consumer_id": dispute["consumer_id"],
        "provider_id": dispute["provider_id"],
        "status": dispute["status"],
        "reason": dispute["reason"],
        "resolution": dispute.get("resolution"),
        "created_at": dispute["created_at"],
        "resolved_at": dispute.get("resolved_at"),
        "escrow_status": escrow.get("status") if escrow else None
    }

@app.post("/disputes/resolve")
async def resolve_dispute(payload: DisputeResolve, x_mep_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_mep_admin_key)
    dispute = db.get_dispute(payload.task_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="No dispute found for task")
    if dispute["status"] != "open":
        raise HTTPException(status_code=409, detail="Dispute is not open")
    resolution = payload.resolution.strip().lower()
    if resolution not in ("consumer", "provider"):
        raise HTTPException(status_code=400, detail="Resolution must be consumer or provider")
    escrow = db.get_escrow(payload.task_id)
    if resolution == "consumer":
        if not escrow or escrow.get("status") != "released":
            raise HTTPException(status_code=400, detail="Escrow not eligible for chargeback")
        chargeback = db.chargeback_escrow(payload.task_id, time.time())
        if chargeback["status"] == "invalid":
            raise HTTPException(status_code=400, detail="Escrow not eligible for chargeback")
        if chargeback["status"] == "insufficient":
            raise HTTPException(status_code=400, detail="Provider lacks funds for chargeback")
        log_audit("DISPUTE_CHARGEBACK", chargeback["consumer_id"], chargeback["amount"], db.get_balance(chargeback["consumer_id"]), payload.task_id)
    if not db.resolve_dispute(payload.task_id, resolution, time.time()):
        raise HTTPException(status_code=404, detail="No open dispute found")
    log_event("dispute_resolved", f"Dispute resolved for task {payload.task_id[:8]} in favor of {resolution}", task_id=payload.task_id, resolution=resolution)
    latest = db.get_dispute(payload.task_id) or dispute
    latest_escrow = db.get_escrow(payload.task_id)
    return {
        "status": "success",
        "task_id": payload.task_id,
        "resolution": resolution,
        "dispute_status": latest.get("status"),
        "escrow_status": latest_escrow.get("status") if latest_escrow else None
    }

@app.get("/health")
async def health_check():
    db_health = db.check_database_health()
    async with node_lock:
        online_count = len(connected_nodes)
    async with registry_reconcile_lock:
        reconcile_snapshot = dict(registry_reconcile_metrics)
    async with task_lock:
        active_count = len(active_tasks)
        completed_count = len(completed_tasks)
    return {
        "status": "ok" if db_health.get("ok") else "degraded",
        "database": db_health,
        "metrics": {
            "connected_nodes": online_count,
            "active_tasks": active_count,
            "completed_task_cache": completed_count,
            "registry_reconcile": reconcile_snapshot,
        }
    }


# ---------------------------------------------------------------------------
# Diagnostic Endpoint 鈥?tiered approach (per Hermes DM discussion)
# ---------------------------------------------------------------------------
class DiagnosticResponse(BaseModel):
    node_id: Optional[str] = None
    registered: bool = False
    availability: Optional[str] = None
    last_heartbeat: Optional[float] = None
    ws_connected: bool = False
    connected_since: Optional[float] = None
    last_ws_activity: Optional[float] = None
    auth_ok: Optional[bool] = None
    error: Optional[str] = None


class OnboardDiagnoseRequest(BaseModel):
    node_id: Optional[str] = None
    registered: Optional[bool] = None
    ws_connected: Optional[bool] = None
    heartbeat_seconds_ago: Optional[float] = None
    auth_status: Optional[str] = None
    dm_status: Optional[str] = None
    listener_contract_ok: Optional[bool] = None
    ai_configured: Optional[bool] = None
    clock_skew_seconds: Optional[float] = None


def _onboard_diagnosis_result(
    root_cause: str,
    severity: str,
    fix_steps: list[str],
    copy_paste_commands: list[str],
) -> dict:
    return {
        "root_cause": root_cause,
        "severity": severity,
        "fix_steps": fix_steps,
        "copy_paste_commands": copy_paste_commands,
    }


def _diagnose_onboarding(payload: OnboardDiagnoseRequest) -> dict:
    auth_status = (payload.auth_status or "").strip().lower()
    dm_status = (payload.dm_status or "").strip().lower()

    if auth_status in {"401", "invalid_signature", "signature_invalid", "timestamp_invalid"}:
        return _onboard_diagnosis_result(
            "auth_401_signature_or_timestamp",
            "high",
            [
                "Regenerate signature using payload + current Unix timestamp.",
                "Verify local clock drift is under 5 minutes.",
                "Confirm Node ID and private key pair match registration public key.",
            ],
            [
                "python -m node.test_crypto",
                "curl -s http://localhost:8000/diagnostic?node_id=<your_node_id>",
            ],
        )

    if auth_status in {"403", "unregistered"} or payload.registered is False:
        return _onboard_diagnosis_result(
            "auth_403_unregistered_or_policy",
            "high",
            [
                "Register node before any signed endpoint usage.",
                "Confirm allowlist and trusted host policy settings.",
                "Retry after registration with fresh signature headers.",
            ],
            [
                "curl -X POST http://localhost:8000/register -H \"Content-Type: application/json\" -d \"{\\\"pubkey\\\":\\\"<PEM>\\\"}\"",
            ],
        )

    if payload.ws_connected is False and payload.heartbeat_seconds_ago is not None and payload.heartbeat_seconds_ago <= 120:
        return _onboard_diagnosis_result(
            "ghost_online_no_ws_presence",
            "high",
            [
                "Restart listener and ensure websocket connection stays active.",
                "Treat websocket connectivity as source of truth for live status.",
                "Keep heartbeat interval steady and shorter than disconnect detection window.",
            ],
            [
                "python -m node.mep_status",
                "curl -s \"http://localhost:8000/diagnostic?node_id=<your_node_id>\"",
            ],
        )

    if dm_status in {"pending", "target_offline", "route_error", "delivery_failed"}:
        return _onboard_diagnosis_result(
            "dm_pending_target_offline_or_route_issue",
            "medium",
            [
                "Verify target node websocket is online before DM.",
                "Check DM target node_id spelling and connectivity.",
                "Retry DM after confirming both nodes are connected.",
            ],
            [
                "python -m node.test_dm",
            ],
        )

    if payload.listener_contract_ok is False:
        return _onboard_diagnosis_result(
            "listener_payload_contract_mismatch",
            "medium",
            [
                "Use authoritative fields expected by Hub APIs (task_id/provider_id/result_payload).",
                "Avoid stub payload responses for completed tasks.",
                "Validate listener request/response schema with integration tests.",
            ],
            [
                "python -m pytest tests/test_hub_api.py -q",
            ],
        )

    if payload.clock_skew_seconds is not None and abs(payload.clock_skew_seconds) > MAX_SKEW_SECONDS:
        return _onboard_diagnosis_result(
            "heartbeat_interval_or_clock_drift",
            "medium",
            [
                "Synchronize host clock with NTP before signing requests.",
                "Keep heartbeat interval stable (for example 30-60s).",
                "Re-run diagnostic checks after clock sync.",
            ],
            [
                "python -m node.mep_status",
            ],
        )

    if payload.ai_configured is False:
        return _onboard_diagnosis_result(
            "ai_provider_config_invalid",
            "low",
            [
                "Configure provider credentials/model in runtime config.",
                "Start with mock adapter to validate runtime before real AI adapter.",
                "Switch to Ollama or OpenAI-compatible adapter after runtime passes doctor checks.",
            ],
            [
                "python -m node.mep_ai_provider",
            ],
        )

    return _onboard_diagnosis_result(
        "healthy_or_insufficient_signal",
        "info",
        [
            "No critical onboarding fault detected from provided snapshot.",
            "If issues persist, include auth_status, dm_status, and websocket state in the next diagnosis call.",
        ],
        [],
    )


@app.get("/diagnostic")
async def diagnostic(
    node_id: Optional[str] = None,
    x_mep_nodeid: Optional[str] = Header(default=None),
    x_mep_timestamp: Optional[str] = Header(default=None),
    x_mep_signature: Optional[str] = Header(default=None),
) -> DiagnosticResponse:
    """
    Tiered diagnostic endpoint.

    Tier 1 (public, no auth) 鈥?specify node_id as query param:
        GET /diagnostic?node_id=node_xxx
        Returns: {node_id, registered, availability, last_heartbeat}

    Tier 2 (authenticated) 鈥?no query param, uses auth headers:
        GET /diagnostic (with X-MEP-NodeID, X-MEP-Timestamp, X-MEP-Signature)
        Returns: full health report including ws_connected, last_ws_activity, auth_ok

    No node_id + no auth -> returns error="usage"
    """
    requesting_node = x_mep_nodeid

    # Tier 1: Public diagnostic with node_id query param
    if node_id:
        registry = db.get_registry(node_id)
        if not registry:
            return DiagnosticResponse(error="node_not_found", node_id=node_id)
        ws_connected = node_id in connected_nodes
        async with node_activity_lock:
            last_ws = node_last_activity.get(node_id)
        return DiagnosticResponse(
            node_id=node_id,
            registered=True,
            availability=registry.get("availability"),
            last_heartbeat=registry.get("updated_at"),
            ws_connected=ws_connected,
            last_ws_activity=last_ws,
        )

    # Tier 2: Authenticated full diagnostic
    if requesting_node:
        ts = x_mep_timestamp or ""
        sig = x_mep_signature or ""
        if not ts or not sig:
            raise HTTPException(status_code=401, detail="Missing authentication fields")
        _validate_timestamp(ts)

        pub_pem = db.get_pub_pem(requesting_node)
        if not pub_pem:
            raise HTTPException(status_code=401, detail="Unknown Node ID. Please register first.")
        if not auth.verify_signature(pub_pem, requesting_node, ts, sig):
            raise HTTPException(status_code=401, detail="Invalid cryptographic signature.")

        ws_connected = requesting_node in connected_nodes
        async with node_activity_lock:
            last_ws = node_last_activity.get(requesting_node)
        registry = db.get_registry(requesting_node)

        return DiagnosticResponse(
            node_id=requesting_node,
            registered=bool(registry),
            availability=registry.get("availability") if registry else None,
            last_heartbeat=registry.get("updated_at") if registry else None,
            ws_connected=ws_connected,
            connected_since=None,
            last_ws_activity=last_ws,
            auth_ok=True,
        )

    # No node_id, no auth -> usage error
    return DiagnosticResponse(error="usage", node_id=None, registered=False, ws_connected=False)


@app.post("/onboard/diagnose")
async def onboard_diagnose(payload: OnboardDiagnoseRequest):
    global onboard_diagnosis_total
    result = _diagnose_onboarding(payload)
    root_cause = result["root_cause"]
    onboard_diagnosis_total += 1
    onboard_diagnosis_counts[root_cause] = int(onboard_diagnosis_counts.get(root_cause, 0)) + 1
    result["telemetry"] = {
        "total_requests": onboard_diagnosis_total,
        "root_cause_count": onboard_diagnosis_counts[root_cause],
    }
    log_event(
        "onboard_diagnose",
        f"Onboarding diagnosis generated: {root_cause}",
        node_id=payload.node_id,
        root_cause=root_cause,
        severity=result["severity"],
    )
    return result


@app.get("/logs/ledger_audit.log", response_class=PlainTextResponse)
async def ledger_audit_log():
    path = _resolve_log_path("ledger_audit.log")
    if not path:
        raise HTTPException(status_code=404, detail="Audit log not found")
    lines = _tail_lines(path, 200)
    return PlainTextResponse("".join(lines))

@app.get("/ledger/entries")
async def ledger_entries(limit: int = 50, authenticated_node: str = Depends(verify_request)):
    safe_limit = max(1, min(limit, 200))
    entries = _read_audit_entries_for_node(authenticated_node, safe_limit)
    return {"node_id": authenticated_node, "entries": entries, "count": len(entries)}

@app.get("/events/recent")
async def recent_events(limit: int = 50, x_mep_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_mep_admin_key)
    safe_limit = max(1, min(limit, 200))
    events = _read_recent_events(safe_limit)
    return {"count": len(events), "events": events}


@app.get("/anti-loop/status")
async def anti_loop_status(channel: Optional[str] = None):
    now = time.time()

    def _serialize(channel_id: str, state: dict) -> dict:
        events = list(state.get("events", []))
        cutoff = now - ANTI_LOOP_WINDOW_SECONDS
        recent = [event for event in events if float(event.get("ts", 0)) >= cutoff]
        cooldown_until = float(state.get("cooldown_until", 0.0) or 0.0)
        return {
            "channel": channel_id,
            "state": "cooldown" if cooldown_until > now else "normal",
            "consecutive_bot_messages": len([event for event in recent if not event.get("termination")]),
            "circuit_break_active": cooldown_until > now,
            "cooldown_remaining_seconds": max(0, int(cooldown_until - now)),
            "circuit_break_count": int(state.get("circuit_break_count", 0)),
            "last_reason": state.get("last_reason"),
            "last_break_at": state.get("last_break_at"),
            "nodes_involved": state.get("last_nodes", []),
        }

    async with anti_loop_lock:
        if channel:
            state = anti_loop_channels.get(channel)
            if not state:
                return {
                    "channel": channel,
                    "state": "normal",
                    "consecutive_bot_messages": 0,
                    "circuit_break_active": False,
                    "cooldown_remaining_seconds": 0,
                    "circuit_break_count": 0,
                    "last_reason": None,
                    "last_break_at": None,
                    "nodes_involved": [],
                }
            return _serialize(channel, state)
        items = [_serialize(channel_id, state) for channel_id, state in anti_loop_channels.items()]
    return {"channels": items, "count": len(items)}


@app.post("/anti-loop/reset")
async def anti_loop_reset(channel: Optional[str] = None, x_mep_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_mep_admin_key)
    async with anti_loop_lock:
        if channel:
            state = anti_loop_channels.get(channel)
            if not state:
                return {"status": "ok", "reset_channels": 0}
            state["cooldown_until"] = 0.0
            state["events"] = []
            state["last_reason"] = "manual_reset"
            state["last_break_at"] = time.time()
            state["last_nodes"] = []
            return {"status": "ok", "reset_channels": 1, "channel": channel}
        anti_loop_channels.clear()
        return {"status": "ok", "reset_channels": "all"}

@app.get("/", response_class=HTMLResponse)
async def hub_landing(request: Request):
    async with node_lock:
        online_count = len(connected_nodes)
    async with task_lock:
        active_count = len(active_tasks)
    uptime_seconds = _get_system_uptime_seconds()
    uptime = _format_uptime(int(uptime_seconds)) if uptime_seconds is not None else "unknown"
    status = "online" if uptime_seconds is not None else "unknown"
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto in ("http", "https"):
        base_url = str(request.base_url).replace(request.url.scheme + "://", f"{forwarded_proto}://", 1).rstrip("/")
    else:
        base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")
    total_nodes = db.get_node_count()
    last_completed_ts = db.get_last_completed_task_time()
    last_completed = datetime.utcfromtimestamp(last_completed_ts).strftime("%Y-%m-%d %H:%M:%S UTC") if last_completed_ts else "-"
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MEP Hub 0</title>
  <style>
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; margin: 0; padding: 20px; color: #111; background-color: #f9fafb; }}
    .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 12px; padding: 24px; max-width: 720px; margin: 0 auto; box-shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1); }}
    .kpi {{ font-size: 32px; font-weight: 700; line-height: 1.2; }}
    .label {{ color: #6b7280; font-size: 14px; font-weight: 500; }}
    .row {{ display: flex; gap: 24px; margin-top: 20px; flex-wrap: wrap; }}
    .section {{ margin-top: 24px; padding-top: 20px; border-top: 1px solid #f3f4f6; }}
    .mono {{ background: #f8fafc; padding: 12px; border-radius: 8px; font-size: 13px; overflow-wrap: break-word; word-break: break-all; border: 1px solid #e2e8f0; }}
    a {{ color: #2563eb; text-decoration: none; font-weight: 500; }}
    a:hover {{ text-decoration: underline; }}
    @media (max-width: 600px) {{
        body {{ padding: 16px; }}
        .card {{ padding: 16px; }}
        .row {{ gap: 16px; }}
        .kpi {{ font-size: 28px; }}
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="label">Welcome to MEP Hub 0</div>
    <div>Version {app.version} Uptime {uptime} Status {status}</div>
    <div class="row">
      <div>
        <div class="kpi">{online_count}</div>
        <div class="label">Bots online</div>
      </div>
      <div>
        <div class="kpi">{active_count}</div>
        <div class="label">Active tasks</div>
      </div>
      <div>
        <div class="kpi">{total_nodes}</div>
        <div class="label">Total nodes registered</div>
      </div>
    </div>
    <div class="section">
      <div class="label">Docs</div>
      <div><a href="https://github.com/WUAIBING/MEP/blob/main/README.md">GitHub README</a></div>
    </div>
    <div class="section">
      <div class="label">How to connect</div>
      <div class="mono">HUB_URL={base_url}<br>WS_URL={ws_url}</div>
    </div>
    <div class="section">
      <div class="label">Health</div>
      <div>
        <a href="#" onclick="checkHealth(event)">Check Status</a>
        <div id="health-status" class="mono" style="display:none; margin-top: 8px;"></div>
      </div>
    </div>
    <div class="section">
      <div class="label">Last task completed</div>
      <div>{last_completed}</div>
    </div>
    <div class="section" style="padding-bottom: 20px;">
      <div class="label">Auth headers</div>
      <div style="word-wrap: break-word;">Requests must include x-mep-nodeid, x-mep-timestamp, x-mep-signature.</div>
    </div>
  </div>
  <script>
    async function checkHealth(e) {{
      e.preventDefault();
      const el = document.getElementById('health-status');
      el.style.display = 'block';
      el.innerText = 'Checking...';
      try {{
        const res = await fetch('{base_url}/health');
        const data = await res.json();
        el.innerText = JSON.stringify(data, null, 2);
        el.style.color = 'green';
      }} catch (err) {{
        el.innerText = 'Error: ' + err.message;
        el.style.color = 'red';
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html)

@app.websocket("/ws/{node_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    node_id: str,
    timestamp: Optional[str] = None,
    signature: Optional[str] = None,
    x_mep_timestamp: Optional[str] = Header(default=None),
    x_mep_signature: Optional[str] = Header(default=None),
):
    client_host = _websocket_client_ip(websocket)
    if not _is_allowed_ip(client_host):
        await websocket.close(code=4003, reason="Client IP not allowed")
        return
    if not _is_trusted_host(websocket.headers.get("host")):
        await websocket.close(code=1008, reason="Untrusted host")
        return
    ws_forwarded_proto = websocket.headers.get("X-Forwarded-Proto", "").lower().strip()
    ws_proto = websocket.url.scheme.lower()
    if TRUST_PROXY_PROTO and ws_forwarded_proto in ("http", "https"):
        ws_proto = "wss" if ws_forwarded_proto == "https" else "ws"
    if REQUIRE_TLS and ws_proto != "wss" and not _is_local_dev_host(websocket.headers.get("host")):
        await websocket.close(code=1008, reason="TLS required")
        return

    ws_timestamp = x_mep_timestamp or timestamp
    ws_signature = x_mep_signature or signature
    if not ws_timestamp or not ws_signature:
        await websocket.close(code=4004, reason="Missing authentication fields")
        return

    try:
        _apply_rate_limit(f"{node_id}:/ws")
        _validate_timestamp(ws_timestamp)
    except HTTPException as exc:
        await websocket.close(code=4004, reason=exc.detail)
        return

    pub_pem = db.get_pub_pem(node_id)
    if not pub_pem:
        await websocket.close(code=4001, reason="Unknown Node ID")
        return

    if not auth.verify_signature(pub_pem, node_id, ws_timestamp, ws_signature):
        await websocket.close(code=4002, reason="Invalid Signature")
        return

    await websocket.accept()
    async with node_lock:
        connected_nodes[node_id] = websocket
    db.update_registry_availability(node_id, "online", time.time())
    try:
        while True:
            await websocket.receive_text()
            await _track_node_activity(node_id)
    except WebSocketDisconnect:
        async with node_lock:
            if connected_nodes.get(node_id) is websocket:
                del connected_nodes[node_id]
        db.update_registry_availability(node_id, "offline", time.time())
