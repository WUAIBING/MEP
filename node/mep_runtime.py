#!/usr/bin/env python3
"""Unified node runtime for fast onboarding (`init`, `up`, `run`, `status`, `doctor`)."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import requests

try:
    from node.identity import MEPIdentity
except ImportError:  # pragma: no cover - supports direct file execution
    from identity import MEPIdentity

try:
    from node.task_envelope import build_task_envelope
except ImportError:  # pragma: no cover - supports direct file execution
    from task_envelope import build_task_envelope

try:
    from clients.shared.mep_client import MEPClient
except ImportError:  # pragma: no cover - direct file execution from node/ may not see repo root
    MEPClient = None  # type: ignore[assignment]

try:
    from clients.shared import identity_paths
except ImportError:  # pragma: no cover - direct file execution from node/ may not see repo root
    identity_paths = None  # type: ignore[assignment]


DEFAULT_HUB_URL = os.getenv("HUB_URL", "http://localhost:8000")
DEFAULT_WS_URL = os.getenv("WS_URL", "ws://localhost:8000")
LEGACY_RUNTIME_KEY_NAME = "mep_runtime.pem"


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) not in ("0", "false", "False", "")


def _strict_adapter_mode() -> bool:
    return _env_truthy("MEP_STRICT_ADAPTERS", "0")


def _review_max_chars() -> int:
    return _env_positive_int("MEP_REVIEW_MAX_CHARS", 4000)


def _review_run_checks_enabled() -> bool:
    return _env_truthy("MEP_REVIEW_RUN_CHECKS", "0")


def _review_trusted_associations() -> set[str]:
    raw = os.getenv("MEP_REVIEW_TRUSTED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR")
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _review_allow_external_checks() -> bool:
    return _env_truthy("MEP_REVIEW_ALLOW_EXTERNAL_CHECKS", "0")


def _is_adapter_error(text: str) -> bool:
    """Detect adapter failures that must never be published as a real review.

    Reviewer runtimes return error sentinels (missing/expired API key, HTTP
    errors, timeouts, empty completions) as plain strings. Treating those as a
    completed review is how an approval can be written back on top of an API
    error, so the bridge must see a failed status instead.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if cleaned.startswith("[DeepSeek]") and ("api error" in lowered or "error:" in lowered or "empty" in lowered):
        return True
    if cleaned.startswith("[AI adapter]") and ("error" in lowered or "empty" in lowered or "timed out" in lowered):
        return True
    return False


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _find_git_root(start_path: Optional[str] = None) -> Optional[str]:
    if identity_paths is not None:
        return identity_paths.find_git_root(start_path)
    current = os.path.abspath(start_path or os.getcwd())
    while True:
        git_marker = os.path.join(current, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _default_key_dir() -> str:
    if identity_paths is not None:
        return identity_paths.default_key_dir()
    explicit = os.getenv("MEP_KEY_DIR")
    if explicit:
        return explicit
    return os.path.join(os.path.expanduser("~"), ".mep")


def _default_key_path() -> str:
    if identity_paths is not None:
        return identity_paths.default_key_path()
    explicit = os.getenv("MEP_PROVIDER_KEY_PATH")
    if explicit:
        return explicit
    return os.path.join(_default_key_dir(), LEGACY_RUNTIME_KEY_NAME)


def _ensure_key_parent(path: str) -> None:
    if identity_paths is not None:
        identity_paths.ensure_key_parent(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _same_path(left: str, right: str) -> bool:
    if identity_paths is not None:
        return identity_paths.same_path(left, right)
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _canonical_key_path(key_dir: str, node_id: str) -> str:
    if identity_paths is not None:
        return identity_paths.canonical_key_path(key_dir, node_id)
    return os.path.join(key_dir, f"{node_id}.pem")


def _enc_key_path(key_path: str) -> str:
    if identity_paths is not None:
        return identity_paths.enc_key_path(key_path)
    return key_path.replace(".pem", "_enc.pem")


def _pending_key_path(key_dir: str) -> str:
    if identity_paths is not None:
        return identity_paths.pending_key_path(key_dir)
    return os.path.join(key_dir, f".pending-runtime-{os.getpid()}-{int(time.time() * 1000)}.pem")


def _is_identity_key_file(filename: str) -> bool:
    if identity_paths is not None:
        return identity_paths.is_identity_key_file(filename)
    return (
        filename.endswith(".pem")
        and not filename.endswith("_enc.pem")
        and not filename.startswith(".pending-runtime-")
    )


def _list_local_identity_key_paths(key_dir: str) -> list[str]:
    if identity_paths is not None:
        return identity_paths.list_local_identity_key_paths(key_dir)
    if not os.path.isdir(key_dir):
        return []
    return [
        os.path.join(key_dir, name)
        for name in sorted(os.listdir(key_dir))
        if _is_identity_key_file(name) and os.path.isfile(os.path.join(key_dir, name))
    ]


def _move_file_if_present(source: str, destination: str) -> None:
    if identity_paths is not None:
        identity_paths.move_file_if_present(source, destination)
        return
    if _same_path(source, destination) or not os.path.exists(source):
        return
    _ensure_key_parent(destination)
    os.replace(source, destination)


def _alias_sidecar_path(key_path: str) -> str:
    if identity_paths is not None:
        return identity_paths.alias_sidecar_path(key_path)
    return f"{key_path}.alias"


def _write_alias_sidecar(key_path: str, alias: str) -> None:
    if identity_paths is not None:
        identity_paths.write_alias_sidecar(key_path, alias)
        return
    with open(_alias_sidecar_path(key_path), "w", encoding="utf-8") as handle:
        handle.write(alias.strip() + "\n")


def _read_alias_sidecar(key_path: str) -> Optional[str]:
    if identity_paths is not None:
        return identity_paths.read_alias_sidecar(key_path)
    path = _alias_sidecar_path(key_path)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        alias = handle.read().strip()
    return alias or None


def _resolve_runtime_alias(key_path: str, cli_alias: Optional[str], *, node_id: str) -> str:
    if cli_alias:
        return cli_alias
    persisted = _read_alias_sidecar(key_path)
    if persisted:
        return persisted
    return node_id


if identity_paths is not None:
    RuntimeKeyPathError = identity_paths.RuntimeKeyPathError
else:
    class RuntimeKeyPathError(ValueError):
        """Raised when runtime identity selection is ambiguous or missing."""


def _canonicalize_local_identity(key_path: str, key_dir: str) -> str:
    if identity_paths is not None:
        return identity_paths.canonicalize_local_identity(key_path, key_dir)
    identity = MEPIdentity(key_path)
    canonical_path = _canonical_key_path(key_dir, identity.node_id)
    if _same_path(key_path, canonical_path):
        return canonical_path

    _move_file_if_present(key_path, canonical_path)
    _move_file_if_present(_enc_key_path(key_path), _enc_key_path(canonical_path))

    source_alias = _alias_sidecar_path(key_path)
    dest_alias = _alias_sidecar_path(canonical_path)
    if os.path.exists(source_alias) and not os.path.exists(dest_alias):
        _move_file_if_present(source_alias, dest_alias)

    return canonical_path


def _choose_existing_local_identity(key_dir: str, cli_alias: Optional[str]) -> Optional[str]:
    if identity_paths is not None:
        return identity_paths.choose_existing_local_identity(key_dir, cli_alias)
    candidates = _list_local_identity_key_paths(key_dir)
    if not candidates:
        return None

    if cli_alias:
        matching = [path for path in candidates if _read_alias_sidecar(path) == cli_alias]
        if len(matching) == 1:
            return _canonicalize_local_identity(matching[0], key_dir)
        if len(matching) > 1:
            raise RuntimeKeyPathError(
                f"multiple local identities in {key_dir} use alias={cli_alias!r}; pass --key-path explicitly"
            )
        if len(candidates) == 1:
            raise RuntimeKeyPathError(
                f"no local identity in {key_dir} matches alias={cli_alias!r}; pass --key-path explicitly"
            )

    if len(candidates) == 1:
        return _canonicalize_local_identity(candidates[0], key_dir)

    raise RuntimeKeyPathError(
        f"multiple local identities found in {key_dir}; pass --key-path or --alias for an existing node"
    )


def _create_new_local_identity(key_dir: str) -> str:
    if identity_paths is not None:
        return identity_paths.create_new_local_identity(key_dir)
    os.makedirs(key_dir, exist_ok=True)
    pending_path = _pending_key_path(key_dir)
    return _canonicalize_local_identity(pending_path, key_dir)


def _resolve_default_runtime_key_path(command: str, cli_alias: Optional[str]) -> str:
    if identity_paths is not None:
        return identity_paths.resolve_identity_key_path(
            explicit_key_path=os.getenv("MEP_PROVIDER_KEY_PATH"),
            alias_hint=cli_alias,
            create_if_missing=command in {"init", "up"},
        )
    explicit = os.getenv("MEP_PROVIDER_KEY_PATH")
    if explicit:
        return explicit

    key_dir = _default_key_dir()
    chosen = _choose_existing_local_identity(key_dir, cli_alias)
    if chosen:
        return chosen

    if command in {"init", "up"}:
        return _create_new_local_identity(key_dir)

    raise RuntimeKeyPathError(
        f"no local identity found in {key_dir}; run `init`/`up` first or pass --key-path explicitly"
    )


def _json_or_none(resp: requests.Response) -> Optional[dict[str, Any]]:
    try:
        return resp.json()
    except ValueError:
        return None


def _safe_request(
    method: str,
    url: str,
    *,
    timeout: float = 10.0,
    json_body: Optional[dict[str, Any]] = None,
    data_body: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, Optional[dict[str, Any]], str]:
    try:
        resp = requests.request(
            method=method,
            url=url,
            timeout=timeout,
            json=json_body,
            data=data_body,
            headers=headers,
        )
        body = _json_or_none(resp)
        raw = resp.text[:500]
        return resp.status_code, body, raw
    except requests.RequestException as exc:
        return 0, None, str(exc)


def _status_badges(diag: dict[str, Any], *, ai_ready: bool) -> dict[str, bool]:
    registered = bool(diag.get("registered"))
    ws_connected = bool(diag.get("ws_connected"))
    availability = str(diag.get("availability") or "").strip().lower()
    live_availability = availability in {"online", "idle", "busy"}
    last_heartbeat = diag.get("last_heartbeat")
    return {
        "REGISTERED": registered,
        "WS_CONNECTED": ws_connected,
        "HEARTBEATING": ws_connected and bool(last_heartbeat),
        "DM_READY": live_availability and ws_connected,
        "AI_READY": ai_ready,
    }


def _heartbeat_seconds_ago(diag: dict[str, Any]) -> Optional[float]:
    last_heartbeat = diag.get("last_heartbeat")
    if last_heartbeat is None:
        return None
    try:
        return max(0.0, time.time() - float(last_heartbeat))
    except (TypeError, ValueError):
        return None


def _build_doctor_snapshot(
    *,
    node_id: str,
    diag: dict[str, Any],
    auth_status: str,
    dm_status: str,
    listener_contract_ok: Optional[bool],
    ai_configured: bool,
    clock_skew_seconds: Optional[float],
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "registered": bool(diag.get("registered")),
        "ws_connected": bool(diag.get("ws_connected")),
        "heartbeat_seconds_ago": _heartbeat_seconds_ago(diag),
        "auth_status": auth_status,
        "dm_status": dm_status,
        "listener_contract_ok": listener_contract_ok,
        "ai_configured": ai_configured,
        "clock_skew_seconds": clock_skew_seconds,
    }


@dataclass
class MockAdapter:
    """Deterministic adapter used as default for fast, stable onboarding."""

    def generate_reply(self, payload: str, task_data: dict[str, Any]) -> str:
        snippet = (payload or "").strip().replace("\n", " ")[:120]
        if not snippet:
            snippet = "<empty>"
        task_id = str(task_data.get("id", ""))[:8]
        try:
            bounty = float(task_data.get("bounty") or 0.0)
        except (TypeError, ValueError):
            bounty = 0.0
        if bounty == 0:
            market = "chat"
            next_step = "DM received by runtime listener."
        elif bounty < 0:
            market = "data"
            next_step = "Data purchase acknowledged by runtime listener."
        else:
            market = "compute"
            next_step = "Switch adapter to ollama/openai-compatible after doctor is green."
        return (
            "MOCK_ADAPTER_OK\n"
            f"task={task_id}\n"
            f"market={market}\n"
            f"summary={snippet}\n"
            f"next={next_step}"
        )


def _interbot_message_from_task_data(task_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    payload = task_data.get("payload")
    if isinstance(payload, str) and payload.strip():
        if MEPClient is not None:
            try:
                _instructions, interbot_message = MEPClient.extract_interbot_instructions(payload)
            except Exception:  # noqa: BLE001
                interbot_message = None
            if isinstance(interbot_message, dict):
                return interbot_message
        try:
            decoded = json.loads(payload)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    return None


def _task_requires_review_prompt(task_data: dict[str, Any]) -> bool:
    interbot_message = _interbot_message_from_task_data(task_data)
    task: Any = task_data.get("task")
    if not isinstance(task, dict) and isinstance(interbot_message, dict):
        task = interbot_message.get("task")
    inputs = task.get("inputs") if isinstance(task, dict) else None
    bridge_metadata = inputs.get("bridge_metadata") if isinstance(inputs, dict) else None
    if isinstance(bridge_metadata, dict) and str(bridge_metadata.get("bridge_id") or "").strip():
        return True

    intent: Any = task_data.get("intent")
    if not isinstance(intent, dict) and isinstance(interbot_message, dict):
        intent = interbot_message.get("intent")
    intent_type = str(intent.get("type") or "").strip() if isinstance(intent, dict) else ""
    return intent_type in {
        "code.review.request",
        "code.review.approve",
        "code.review.comment",
        "analysis.request",
        "issue.triage.request",
    }


def _repo_audit_inputs(task_data: dict[str, Any]) -> dict[str, Any]:
    interbot_message = _interbot_message_from_task_data(task_data)
    task: Any = task_data.get("task")
    if not isinstance(task, dict) and isinstance(interbot_message, dict):
        task = interbot_message.get("task")
    inputs = task.get("inputs") if isinstance(task, dict) else None
    repo_audit = inputs.get("repo_audit") if isinstance(inputs, dict) else None
    return repo_audit if isinstance(repo_audit, dict) else {}


def _task_title(task_data: dict[str, Any]) -> str:
    interbot_message = _interbot_message_from_task_data(task_data)
    task: Any = task_data.get("task")
    if not isinstance(task, dict) and isinstance(interbot_message, dict):
        task = interbot_message.get("task")
    return str(task.get("title") or "").strip() if isinstance(task, dict) else ""


def _task_expected_output(task_data: dict[str, Any]) -> dict[str, Any]:
    interbot_message = _interbot_message_from_task_data(task_data)
    task: Any = task_data.get("task")
    if not isinstance(task, dict) and isinstance(interbot_message, dict):
        task = interbot_message.get("task")
    expected_output = task.get("expected_output") if isinstance(task, dict) else None
    return expected_output if isinstance(expected_output, dict) else {}


def _task_model_requirement(task_data: dict[str, Any]) -> str:
    return str(task_data.get("model_requirement") or "").strip().lower()


def _task_requires_repo_audit_contract(task_data: dict[str, Any]) -> bool:
    if _review_intent_type(task_data) == "repo_audit.request":
        return True
    if _task_model_requirement(task_data) == "repo_audit":
        return True
    if _task_title(task_data).lower().startswith("repo audit:"):
        return True
    expected_output = _task_expected_output(task_data)
    return str(expected_output.get("result_type") or "").strip().lower() == "repo_audit_result"


def _task_requires_repo_audit_prompt(task_data: dict[str, Any]) -> bool:
    return _task_requires_repo_audit_contract(task_data) and bool(_repo_audit_inputs(task_data))


def _repo_audit_contract_failure(task_data: dict[str, Any]) -> Optional[str]:
    if not _task_requires_repo_audit_contract(task_data):
        return None
    repo_audit = _repo_audit_inputs(task_data)
    if not repo_audit:
        return "[repo audit] missing structured repo_audit inputs; refusing ungrounded audit"
    repo_url = str(repo_audit.get("repo_url") or "").strip()
    if not repo_url:
        return "[repo audit] repo_url missing from structured repo_audit inputs"
    return None


def _review_github_inputs(task_data: dict[str, Any]) -> dict[str, Any]:
    interbot_message = _interbot_message_from_task_data(task_data)
    task: Any = task_data.get("task")
    if not isinstance(task, dict) and isinstance(interbot_message, dict):
        task = interbot_message.get("task")
    inputs = task.get("inputs") if isinstance(task, dict) else None
    github_inputs = inputs.get("github") if isinstance(inputs, dict) else None
    return github_inputs if isinstance(github_inputs, dict) else {}


def _review_intent_type(task_data: dict[str, Any]) -> str:
    interbot_message = _interbot_message_from_task_data(task_data)
    intent: Any = task_data.get("intent")
    if not isinstance(intent, dict) and isinstance(interbot_message, dict):
        intent = interbot_message.get("intent")
    return str(intent.get("type") or "").strip().lower() if isinstance(intent, dict) else ""


def _task_is_approval_review(task_data: dict[str, Any]) -> bool:
    return _review_intent_type(task_data) == "code.review.approve"


def _review_mode_for_task(task_data: dict[str, Any]) -> str:
    github_inputs = _review_github_inputs(task_data)
    review_mode = _clean_review_label(github_inputs.get("review_mode"), max_chars=40).lower()
    if review_mode == "recheck_review":
        return "recheck_review"
    return "discovery_review"


def _review_lenses_for_task(task_data: dict[str, Any]) -> list[str]:
    github_inputs = _review_github_inputs(task_data)
    touched_paths = _clean_review_list(github_inputs.get("touched_paths"), max_items=8, max_chars=120)
    touched_tests = _clean_review_list(github_inputs.get("touched_tests"), max_items=6, max_chars=120)
    risk_pack = github_inputs.get("risk_pack")
    risk_pack = risk_pack if isinstance(risk_pack, dict) else {}

    signal_parts: list[str] = []
    for collection in (
        touched_paths,
        touched_tests,
        _clean_review_list(risk_pack.get("changed_identifiers"), max_items=12, max_chars=80),
        _clean_review_list(risk_pack.get("touched_non_test_paths"), max_items=8, max_chars=120),
    ):
        signal_parts.extend(collection)
    signal_blob = " ".join(part.lower() for part in signal_parts if part)

    lenses: list[str] = []

    def _add_lens(label: str) -> None:
        if label and label not in lenses:
            lenses.append(label)

    _add_lens("correctness/regression around the changed behavior")
    if touched_tests:
        _add_lens("test alignment and edge-case coverage for the changed behavior")
    else:
        _add_lens("missing or misaligned test coverage for the changed behavior")

    if any(token in signal_blob for token in ("auth", "token", "permission", "signature", "webhook", "bridge", "github", "identity", "verify", "approval")):
        _add_lens("security/trust-boundary regressions and approval safety")
    if any(token in signal_blob for token in ("async", "await", "lock", "queue", "session", "retry", "timeout", "poll", "heartbeat", "call_relay", "ws", "websocket")):
        _add_lens("state transitions, retry paths, async sequencing, or concurrency hazards")
    if any(token in signal_blob for token in ("db", "schema", "migration", "storage", "persist", "json", "serialize", "deserialize", "ledger", "backfill", "model")):
        _add_lens("data persistence, migration, compatibility, or serialization drift")
    if any(path.lower().startswith("bridge/") for path in touched_paths):
        _add_lens("automation/writeback safety and path-to-action mismatches")
    if len(lenses) < 4:
        _add_lens("API contract, validation, and fallback-path drift")
    return lenses[:5]


def _repo_audit_lenses_for_task(task_data: dict[str, Any]) -> list[str]:
    repo_audit = _repo_audit_inputs(task_data)
    repo_url = _clean_review_label(repo_audit.get("repo_url"), max_chars=200).lower()
    inventory_paths = _clean_review_list(repo_audit.get("inventory_paths"), max_items=300, max_chars=160)
    inventory_blob = " ".join(path.lower() for path in inventory_paths)
    signal_blob = f"{repo_url} {inventory_blob}".strip()

    lenses: list[str] = []

    def _add_lens(label: str) -> None:
        if label and label not in lenses:
            lenses.append(label)

    _add_lens("fail-closed correctness around audit contracts, missing inputs, or fallback paths")
    _add_lens("cross-file state or metadata drift that can break real execution paths")

    if any(token in signal_blob for token in ("github_to_mep", "bridge/", "approval", "ci", "pull", "review")):
        _add_lens("automation/writeback safety, approval gating, and path-to-action mismatches")
    if any(token in signal_blob for token in ("mep_runtime", "repo-audit", "workspace", "inventory", "runtime", "node/")):
        _add_lens("workspace grounding, runtime honesty, and inventory-backed publication")
    if any(token in signal_blob for token in ("hub/main.py", "hub/db.py", "core/ledger", "ledger", "economics", "bounty")):
        _add_lens("ledger, persistence, or state-integrity drift across storage and execution")
    if any(token in signal_blob for token in ("hub/auth.py", "node/identity.py", "signature", "auth", "identity", "verify")):
        _add_lens("auth, identity, malformed-input handling, and trust-boundary regressions")
    if any(token in signal_blob for token in ("deployment", "docker-compose", "version", "build", "scripts/deploy")):
        _add_lens("deploy provenance, version drift, and config/runtime mismatch")
    if any("/test" in token or token.startswith("tests/") or token.endswith("_test.py") for token in inventory_paths):
        _add_lens("test coverage or verification gaps around high-risk behaviors")
    if len(lenses) < 5:
        _add_lens("config, migration, or compatibility drift that can break the next change")
    return lenses[:6]


def _candidate_priority_rank(priority: str) -> int:
    normalized = str(priority or "").strip().lower()
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return order.get(normalized, order["medium"])


def _clean_review_list(values: Any, *, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        entry = _clean_review_label(item, max_chars=max_chars)
        if not entry:
            continue
        key = entry.lower()
        if key in seen:
            continue
        cleaned.append(entry)
        seen.add(key)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _filter_review_list_to_allowed(values: list[str], allowed: list[str]) -> list[str]:
    if not values:
        return []
    if not allowed:
        return values
    allowed_map = {item.lower(): item for item in allowed if item}
    filtered: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = item.lower()
        matched = allowed_map.get(normalized)
        if not matched or normalized in seen:
            continue
        filtered.append(matched)
        seen.add(normalized)
    return filtered


def _system_prompt_for_task(
    task_data: dict[str, Any],
    *,
    generic_max_chars: int,
    review_max_chars: int,
) -> str:
    if _task_requires_repo_audit_prompt(task_data):
        repo_audit = _repo_audit_inputs(task_data)
        repo_url = _clean_review_label(repo_audit.get("repo_url"), max_chars=200) or "the supplied repository"
        ref = _clean_review_label(repo_audit.get("ref"), max_chars=120)
        workspace_path = _clean_review_label(repo_audit.get("local_workspace_path"), max_chars=220)
        inventory_paths = _clean_review_list(repo_audit.get("inventory_paths"), max_items=300, max_chars=160)
        audit_lenses = _repo_audit_lenses_for_task(task_data)
        workspace_hint = ""
        if workspace_path:
            workspace_hint = (
                f" A checked-out local workspace is available at `{workspace_path}`. The user message may include an "
                "authoritative repo inventory and selected file contents from that workspace; treat only that material as "
                "ground truth."
            )
        inventory_hint = ""
        if inventory_paths:
            inventory_hint = (
                f" The authoritative repo inventory currently contains {len(inventory_paths)} tracked files. "
                "Every finding must cite one file from that supplied inventory; if you cannot verify a concrete issue "
                "against those files, return zero findings."
            )
        audit_lenses_hint = (
            "Before generic security scanning, pressure-test these repository-specific audit lenses in priority order: "
            + "; ".join(audit_lenses[:5])
            + ". "
            if audit_lenses
            else ""
        )
        ref_hint = f" at ref `{ref}`" if ref else ""
        return (
            f"You are a senior repository auditor reviewing {repo_url}{ref_hint}. "
            "Return ONLY a JSON object with this schema: "
            '{"summary": string, "repo_overview": string, "coverage_summary": string, '
            '"files_deep_read": [string], "areas_not_deeply_reviewed": [string], '
            '"risk_areas_checked": [string], "checks_performed": [string], "why_no_finding": string, '
            '"findings": [{"file": string, "title": string, "category": string, "severity": "high|medium|low", '
            '"confidence": "high|medium|low", "invariant": string, "failure_mode": string, "proof_type": string, '
            '"fix_priority": "fix_now|next_change|later", "developer_impact": string, "evidence": string, '
            '"supporting_files": [string], "same_file_check": string, "same_file_contradicted_by": [string], '
            '"contradiction_check": string, "contradicted_by": [string], "next_step": string}], '
            '"near_misses": [{"file": string, "title": string, "reason_not_published": string}], '
            '"observations": [{"file": string, "note": string}], '
            '"artifact_recommended": boolean}. '
            "Publish at most 5 findings, ranked by developer impact. "
            "Use `findings` only for high-signal issues that a developer should fix now or on the next change in that area. "
            "Every finding must be invariant-backed: state the expected invariant, the concrete failure mode, the proof type, and the fix priority. "
            "Put lower-signal hygiene notes, uncertainty, or weakly actionable comments into `observations` instead of `findings`. "
            "Use `repo_overview` for a concise factual description of the repository areas you actually inspected. "
            "Use `files_deep_read` for the exact tracked files you actually read closely, and `areas_not_deeply_reviewed` for the important areas you did not inspect deeply. "
            "Keep `coverage_summary` tightly grounded in those structured coverage fields; do not claim broad coverage you cannot support. "
            "Use `checks_performed` for 1-5 concrete verification steps you performed against the supplied inventory, file contents, or verification output. "
            "Use `risk_areas_checked` for 1-5 concrete risk themes you audited. "
            "Only publish a finding when the supplied repo context directly supports it. "
            "Every published finding must name the impacted file, explain the developer impact, cite concrete evidence from the supplied repo context, and give one short next step. "
            "For every high-severity finding, include `supporting_files` with the impacted file plus at least one additional deep-read file that proves the caller, guard, or enforcement path. "
            "For every high-severity finding, fill `same_file_check` with the exact guard, branch, condition, or identifier from the impacted file that you checked to rule out a local contradiction. "
            "If the impacted file itself contains a nearby guard, branch, or identifier that weakens the claim, list it in `same_file_contradicted_by` and demote the claim into `near_misses` instead of publishing it as a finding. "
            "Use `contradiction_check` to say which neighboring guard, caller, or enforcement path you checked for contradictory evidence before publishing the claim. "
            "If any supplied file or identifier materially weakens the claim, list it in `contradicted_by` and demote the claim into `near_misses` instead of publishing it as a finding. "
            "If a candidate issue is plausible but did not clear the publication bar, put it in `near_misses` with a short reason instead of promoting it to a finding. "
            f"{audit_lenses_hint}"
            "Do not invent files, endpoints, dependencies, or artifacts that are not present in the supplied repo context. "
            "Do not rely on web knowledge, generic security checklists, or assumptions about unseen code. "
            "If you cannot verify a concrete finding from the supplied workspace material, keep `findings` empty and explain why in `why_no_finding`. "
            "Do not include chain-of-thought or any text outside the JSON object. "
            f"Keep the response within {review_max_chars} characters.{workspace_hint}{inventory_hint}"
        )
    if _task_requires_review_prompt(task_data):
        github_inputs = _review_github_inputs(task_data)
        approval_mode = _task_is_approval_review(task_data)
        review_mode = _review_mode_for_task(task_data)
        workspace_path = str(github_inputs.get("local_workspace_path") or "").strip()
        workspace_hint = ""
        if workspace_path:
            workspace_hint = (
                f" A checked-out local workspace is available at `{workspace_path}`. The input may include the full "
                "contents of the changed files and automated verification (test/lint) results from the PR head commit; "
                "treat that material as the authoritative code context. Base findings on what the full files and "
                "verification output actually show. Do not claim code is truncated, missing, or a placeholder because "
                "of how the input is formatted or because a file body was shortened for length."
            )
        review_mode_hint = ""
        if review_mode == "recheck_review":
            review_mode_hint = (
                " Review mode is `recheck_review`. Treat this as a follow-up verification pass on a PR that was already reviewed once. "
                "Bias toward confirming whether the current diff still leaves a concrete blocker, disproving stale or weak earlier claims, "
                "and checking whether the changed tests and enforcement paths now line up. Do not invent fresh low-signal concerns just to say something new."
            )
        else:
            review_mode_hint = (
                " Review mode is `discovery_review`. Treat this as the first serious review pass and spend your budget hunting for the single "
                "highest-value correctness, regression, edge-case, or missing-validation issue before you summarize."
            )
        approval_hint = ""
        if approval_mode:
            approval_hint = (
                " Approval mode is active. Only use `approval_recommendation: \"approve\"` when you can cite at least two exact identifiers from changed lines "
                "in `verified_identifiers`, mention the changed tests when any are provided, and explicitly state the scope is low-risk. "
                "If the supplied PR checks are pending or failing, use `comment` instead of `approve`. "
                "If any finding survives verification, use `comment` instead of `approve`. "
                "If you cannot satisfy that evidence bar, use `comment` instead of `approve`."
            )
        return (
            "You are a senior code reviewer for the MEP (Miao Exchange Protocol) project. "
            "Your primary job is to find the highest-value correctness, regression, edge-case, or missing-validation risk in the supplied PR context before you summarize anything. "
            "Review the provided GitHub PR context and return ONLY a JSON object with this schema: "
            '{"summary": string, "observation": string, "touched_paths": [string], "tests_reviewed": [string], '
            '"risk_areas_checked": [string], "checks_performed": [string], '
            '"verified_identifiers": [string], '
            '"findings": [{"file": string, "issue": string, "rationale": string}], '
            '"approval_recommendation": "approve" | "comment" | "request_changes" | "abstain"}. '
            "Use at most 2 findings. "
            "Use `observation` for one concrete non-blocking review note tied to the actual diff. "
            "Use `risk_areas_checked` for 1-4 concrete risk themes you examined in the changed code. "
            "Use `checks_performed` for 1-4 specific verification steps you actually performed against the diff, tests, or workspace excerpts. "
            "When findings are empty, keep `summary` verdict-style and `observation` concrete; do not add filler sections or generic praise. "
            "For no-finding reviews, never use generic approval language like `looks good`, `well-structured`, `clean patch`, or `LGTM`. "
            "Use `verified_identifiers` for exact function/variable/class names copied from changed lines in the supplied diff or workspace excerpts. "
            "Prefer real touched files and tests from the supplied GitHub inputs for `touched_paths` and `tests_reviewed`. "
            "For no-finding reviews, always anchor the output to the actual diff: include at least one touched path in `touched_paths`, "
            "include 1-3 exact changed-line identifiers in `verified_identifiers` when the review context provides them, "
            "and make `summary` and `observation` explicitly about that changed behavior rather than generic quality praise. "
            "If you mention code behavior in `summary` or `observation`, tie it to the same touched path or changed identifiers. "
            "Do not claim a helper is missing validation, checks, or guards unless the changed lines themselves show that absence rather than a nearby allowlist, raise, or verification branch. "
            "Do not publish a runtime-exception finding unless the changed lines expose the exact operator, method call, or data-shape transition that would fail under Python semantics. "
            "Do not infer `TypeError`, `unhashable`, or similar hashability failures from ordinary `in`/`not in` membership checks that are only validating an optional value against an allowlist or classification set. "
            "Only include a finding when it is directly supported by the provided diff, file list, PR description, or patch excerpts. "
            "Do not speculate about unseen code, do not ask for more context, and do not include chain-of-thought or any text outside the JSON object. "
            "If the change looks good, keep findings empty, use summary to state the overall conclusion, list the risk areas and checks you covered, keep observation concrete, and set approval_recommendation to approve or comment. "
            "Diff restatement without risk coverage is not a sufficient review. Keep the "
            f"response within {review_max_chars} characters.{review_mode_hint}{approval_hint}{workspace_hint}"
        )
    return (
        "You are a helpful MEP (Miao Exchange Protocol) bot. "
        "MEP is an AI-to-AI economy protocol where agents earn SECONDS by doing work. "
        f"Reply concisely (max {generic_max_chars} chars)."
    )


def _candidate_system_prompt_for_task(task_data: dict[str, Any], *, review_max_chars: int) -> str:
    if not _task_requires_review_prompt(task_data):
        return _system_prompt_for_task(task_data, generic_max_chars=500, review_max_chars=review_max_chars)
    review_mode = _review_mode_for_task(task_data)
    mode_hint = (
        "This is a recheck, so prefer verifying unresolved regression hypotheses, stale review claims, and test/enforcement alignment over opening a brand-new low-signal search space. "
        if review_mode == "recheck_review"
        else "This is a discovery pass, so optimize for finding the strongest previously-unreported bug or regression risk first. "
    )
    return (
        "You are the candidate-generation pass for a MEP GitHub code review. "
        "Scan the supplied PR context, diff excerpts, risk pack, workspace context, and verification output. "
        "Return ONLY a JSON object with this schema: "
        '{"risk_candidates": [{"file": string, "category": string, "priority": "high" | "medium" | "low", "claim": string, "reason": string, "evidence": [string]}], "coverage": [string]}. '
        "Generate at most 4 candidate risks. Use the supplied review lenses to diversify the search space. "
        "Prefer at most one candidate per lens, rank the highest-impact candidate first, and bias toward correctness, trust-boundary, state-transition, rollback, migration, and test-gap risks over style comments. "
        "Each candidate must be concrete, tied to a changed file, and phrased as a potential bug or regression worth verifying. "
        "Use `evidence` for 1-3 exact identifiers, tests, or changed behaviors from the supplied context that make the hypothesis worth verifying. "
        f"{mode_hint}"
        "Do not summarize the PR. Do not give praise. Do not include chain-of-thought. "
        "If nothing looks risky enough to verify, return an empty `risk_candidates` list and use `coverage` to note what you inspected."
    )


def _verification_system_prompt_for_task(task_data: dict[str, Any], *, review_max_chars: int) -> str:
    base = _system_prompt_for_task(task_data, generic_max_chars=500, review_max_chars=review_max_chars)
    if not _task_requires_review_prompt(task_data):
        return base
    review_mode = _review_mode_for_task(task_data)
    mode_hint = (
        "Because this is `recheck_review`, explicitly look for evidence that earlier concerns were fixed, contradicted by the current diff, or demoted by the changed tests and enforcement path. "
        if review_mode == "recheck_review"
        else "Because this is `discovery_review`, prioritize confirming the strongest fresh candidate rather than spreading attention across many medium-signal ideas. "
    )
    return (
        f"{base} This is the verification pass. The user message may include provisional candidate risks from an earlier pass. "
        "Treat those candidates as hypotheses only. Promote a candidate into `findings` only when the supplied diff, workspace context, tests, or verification output directly support it. "
        "For every published finding, set `file` to one of the supplied touched paths and include at least one exact changed-line identifier in `verified_identifiers` that supports the claim. "
        "Prefer the single highest-impact verified finding over multiple medium-signal findings. Publish a second finding only when it is independent, well-supported, and similarly important. "
        "Downgrade or discard candidates that are weakly evidenced, redundant with a stronger finding, or true but too low-value to be a meaningful review comment. "
        "Reject any candidate that relies only on nearby file context, naming similarity, or speculation. "
        "If no candidate survives verification, keep `findings` empty, explain the checks you performed, and keep the summary grounded in the reviewed changed behavior. "
        "For that no-finding case, still return a grounded review: populate `touched_paths` from the real diff, populate `verified_identifiers` from changed lines when available, "
        "and make `summary` and `observation` cite the reviewed changed behavior instead of generic praise. "
        "If `summary` or `observation` says a helper lacks validation, guard logic, or checks, that claim must survive nearby contradiction from the changed lines themselves. "
        "If a candidate predicts a Python runtime exception, verify the exact failing operator or method call from the changed lines before promoting it. "
        "Do not promote `TypeError` or `unhashable` claims that are based only on ordinary allowlist membership checks for optional values. "
        f"{mode_hint}"
        "In approval mode, any surviving finding must force `approval_recommendation` away from `approve`."
    )


def _extract_first_json_object(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        value = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    return None


def _clean_review_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        clipped = text[:max_chars].rstrip()
        sentence_break = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
        if sentence_break >= max(40, max_chars // 2):
            clipped = clipped[: sentence_break + 1].rstrip()
        text = clipped
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _clean_review_label(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text.rstrip(".,;: ")


def _review_text_has_anchor(text: str, *, touched_paths: list[str], identifiers: list[str]) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    for path in touched_paths:
        token = str(path or "").strip().lower()
        if token and token in lowered:
            return True
    for identifier in identifiers:
        token = str(identifier or "").strip().lower()
        if token and token in lowered:
            return True
    return False


def _approval_detail_supports_publishable_approval(
    detail: Optional[str],
    *,
    task_data: Optional[dict[str, Any]],
) -> bool:
    text = str(detail or "").strip()
    if not text:
        return False
    if "## Review Findings" in text:
        return False
    if "## Review Summary" not in text or "Touched paths reviewed:" not in text:
        return False
    if "Checks performed:" not in text or "Risk areas checked:" not in text:
        return False
    github_inputs = _review_github_inputs(task_data or {})
    touched_tests = _clean_review_list(github_inputs.get("touched_tests"), max_items=3, max_chars=120)
    risk_pack = github_inputs.get("risk_pack")
    changed_identifiers = (
        _clean_review_list(risk_pack.get("changed_identifiers"), max_items=8, max_chars=80)
        if isinstance(risk_pack, dict)
        else []
    )
    touched_paths = _clean_review_list(github_inputs.get("touched_paths"), max_items=4, max_chars=120)
    if touched_tests and "Tests reviewed:" not in text:
        return False
    if changed_identifiers:
        if "Changed identifiers verified:" not in text:
            return False
        if not _review_text_has_anchor(
            text,
            touched_paths=touched_paths,
            identifiers=changed_identifiers,
        ):
            return False
    return True


def _extract_review_candidates(text: str) -> list[dict[str, str]]:
    parsed = _extract_first_json_object(text)
    if not isinstance(parsed, dict):
        return []
    raw_candidates = parsed.get("risk_candidates")
    if not isinstance(raw_candidates, list):
        return []
    cleaned: list[tuple[int, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_candidates[:4]):
        if not isinstance(item, dict):
            continue
        file_name = _clean_review_label(item.get("file"), max_chars=120)
        category = _clean_review_label(item.get("category"), max_chars=80) or "correctness/regression"
        priority = _clean_review_label(item.get("priority"), max_chars=20).lower() or "medium"
        if priority not in {"critical", "high", "medium", "low"}:
            priority = "medium"
        claim = _clean_review_text(item.get("claim"), max_chars=220)
        reason = _clean_review_text(item.get("reason"), max_chars=240)
        evidence = _clean_review_list(item.get("evidence"), max_items=3, max_chars=120)
        if not claim:
            continue
        key = (file_name.lower(), claim.lower())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            (
                index,
                {
                    "file": file_name,
                    "category": category,
                    "priority": priority,
                    "claim": claim,
                    "reason": reason,
                    "evidence": evidence,
                },
            )
        )
    cleaned.sort(key=lambda entry: (_candidate_priority_rank(entry[1]["priority"]), entry[0]))
    return [entry[1] for entry in cleaned]


def _payload_with_review_lenses(payload: str, review_lenses: list[str]) -> str:
    base = (payload or "").strip()
    if not review_lenses:
        return base
    lenses_json = json.dumps({"review_lenses": review_lenses}, ensure_ascii=True)
    return (
        f"{base}\n\n"
        "Review lenses to cover before publishing:\n"
        f"{lenses_json}"
    ).strip()


def _candidate_payload_for_verification(
    payload: str,
    candidates: list[dict[str, Any]],
    *,
    review_lenses: list[str],
) -> str:
    base = _payload_with_review_lenses(payload, review_lenses)
    if not candidates:
        return base
    candidate_json = json.dumps({"risk_candidates": candidates}, ensure_ascii=True)
    return (
        f"{base}\n\n"
        "Candidate risks to verify before publishing any finding:\n"
        f"{candidate_json}"
    ).strip()


def _extract_repo_audit_candidate_packet(text: str, *, allowed_paths: list[str]) -> dict[str, list[Any]]:
    parsed = _extract_first_json_object(text)
    if not isinstance(parsed, dict):
        return {"risk_candidates": [], "coverage": []}
    raw_candidates = parsed.get("risk_candidates")
    if not isinstance(raw_candidates, list):
        raw_candidates = []
    coverage = _clean_review_list(parsed.get("coverage"), max_items=6, max_chars=140)
    allowed_map = {path.lower(): path for path in allowed_paths}
    cleaned: list[tuple[int, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_candidates[:4]):
        if not isinstance(item, dict):
            continue
        file_name = _clean_review_label(item.get("file"), max_chars=160)
        matched = allowed_map.get(file_name.lower()) if file_name else None
        if not matched:
            continue
        category = _clean_review_label(item.get("category"), max_chars=80) or "correctness"
        priority = _clean_review_label(item.get("priority"), max_chars=20).lower() or "medium"
        if priority not in {"critical", "high", "medium", "low"}:
            priority = "medium"
        claim = _clean_review_text(item.get("claim"), max_chars=240)
        reason = _clean_review_text(item.get("reason"), max_chars=260)
        evidence = _clean_review_list(item.get("evidence"), max_items=3, max_chars=120)
        if not claim:
            continue
        key = (matched.lower(), claim.lower())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            (
                index,
                {
                    "file": matched,
                    "category": category,
                    "priority": priority,
                    "claim": claim,
                    "reason": reason,
                    "evidence": evidence,
                },
            )
        )
    cleaned.sort(key=lambda entry: (_candidate_priority_rank(entry[1]["priority"]), entry[0]))
    return {"risk_candidates": [entry[1] for entry in cleaned], "coverage": coverage}


def _extract_repo_audit_candidates(text: str, *, allowed_paths: list[str]) -> list[dict[str, Any]]:
    return _extract_repo_audit_candidate_packet(text, allowed_paths=allowed_paths)["risk_candidates"]


def _payload_with_repo_audit_lenses(payload: str, repo_audit_lenses: list[str]) -> str:
    base = (payload or "").strip()
    if not repo_audit_lenses:
        return base
    lenses_json = json.dumps({"repo_audit_lenses": repo_audit_lenses}, ensure_ascii=True)
    return (
        f"{base}\n\n"
        "Repo-audit lenses to cover before publishing:\n"
        f"{lenses_json}"
    ).strip()


def _candidate_payload_for_repo_audit_verification(
    payload: str,
    candidates: list[dict[str, Any]],
    *,
    repo_audit_lenses: list[str],
    coverage: Optional[list[str]] = None,
) -> str:
    base = _payload_with_repo_audit_lenses(payload, repo_audit_lenses)
    if not candidates and not coverage:
        return base
    packet: dict[str, Any] = {}
    if candidates:
        packet["risk_candidates"] = candidates
    cleaned_coverage = _clean_review_list(coverage or [], max_items=6, max_chars=140)
    if cleaned_coverage:
        packet["candidate_coverage"] = cleaned_coverage
    candidate_json = json.dumps(packet, ensure_ascii=True)
    return (
        f"{base}\n\n"
        "Candidate repo-audit material to verify before publishing any finding:\n"
        f"{candidate_json}"
    ).strip()


def _run_two_pass_review(
    *,
    task_data: dict[str, Any],
    payload: str,
    review_max_chars: int,
    invoke_model: Any,
) -> str:
    review_lenses = _review_lenses_for_task(task_data)
    candidate_payload = _payload_with_review_lenses(payload, review_lenses)
    candidate_reply = invoke_model(_candidate_system_prompt_for_task(task_data, review_max_chars=review_max_chars), candidate_payload)
    candidates = _extract_review_candidates(candidate_reply)
    verification_payload = _candidate_payload_for_verification(payload, candidates, review_lenses=review_lenses)
    final_reply = invoke_model(_verification_system_prompt_for_task(task_data, review_max_chars=review_max_chars), verification_payload)
    rendered = _render_structured_review_with_task_data(final_reply, max_chars=review_max_chars, task_data=task_data)
    if rendered:
        return rendered
    finalized = _finalize_model_reply(final_reply, max_chars=review_max_chars)
    if finalized:
        return finalized
    return final_reply[:review_max_chars].rstrip() or "[review runtime] review response was empty"


def _candidate_system_prompt_for_repo_audit(task_data: dict[str, Any], *, review_max_chars: int) -> str:
    if not _task_requires_repo_audit_prompt(task_data):
        return _system_prompt_for_task(task_data, generic_max_chars=500, review_max_chars=review_max_chars)
    return (
        "You are the candidate-generation pass for a MEP repository audit. "
        "Scan the supplied checked-out workspace context, tracked-file inventory, selected file contents, and any verification output. "
        "Return ONLY a JSON object with this schema: "
        '{"risk_candidates": [{"file": string, "category": string, "priority": "high" | "medium" | "low", "claim": string, "reason": string, "evidence": [string]}], "coverage": [string]}. '
        "Generate at most 4 candidate risks. Use the supplied repo-audit lenses to diversify the search space. "
        "Prefer at most one candidate per lens, rank the highest-impact candidate first, and bias toward fail-closed correctness, cross-file drift, trust-boundary errors, deploy mismatch, ledger integrity, and missing verification over hygiene notes. "
        "Each candidate must be concrete, tied to one tracked file from the supplied inventory, and phrased as a plausible developer-facing failure worth verifying. "
        "Use `evidence` for 1-3 exact identifiers, files, or changed behaviors from the supplied workspace context that make the hypothesis worth verifying. "
        "Use `coverage` for the exact files, code paths, or checks you already inspected closely enough to justify a deeper verification pass next. "
        "Do not summarize the repo. Do not give praise. Do not include chain-of-thought. "
        "If nothing looks risky enough to verify, return an empty `risk_candidates` list and use `coverage` to note what you inspected."
    )


def _verification_system_prompt_for_repo_audit(task_data: dict[str, Any], *, review_max_chars: int) -> str:
    base = _system_prompt_for_task(task_data, generic_max_chars=500, review_max_chars=review_max_chars)
    if not _task_requires_repo_audit_prompt(task_data):
        return base
    return (
        f"{base} This is the verification pass. The user message may include provisional candidate risks from an earlier pass. "
        "Treat those candidates as hypotheses only. Promote a candidate into `findings` only when the supplied workspace context directly supports it. "
        "Treat any candidate coverage hints as provisional evidence about what deserves deeper reading, then turn that into grounded `files_deep_read` and `checks_performed`. "
        "For every published finding, set `file` to one tracked file from the supplied inventory and make the finding invariant-backed, failure-specific, and developer-impactful. "
        "Every published finding file must also appear in `files_deep_read`. "
        "Every published finding must cite at least one exact identifier, config key, test, or code path from the supplied workspace context in `evidence`; generic statements like 'this file controls the path' are not enough. "
        "For every high-severity finding, include `supporting_files` with the impacted file plus at least one additional deep-read file that proves the caller, guard, or enforcement path. "
        "Also fill `same_file_check` with the exact guard, branch, condition, or identifier from the impacted file that you checked to rule out a same-file contradiction, and fill `contradiction_check` with the contradictory guard or caller path you checked before publishing. "
        "If `same_file_contradicted_by` is non-empty, do not publish that claim as a finding; move it to `near_misses` instead. "
        "If `contradicted_by` is non-empty, do not publish that claim as a finding; move it to `near_misses` instead. "
        "Prefer one to three high-value verified findings over a longer list of generic concerns. "
        "Downgrade or discard candidates that are weakly evidenced, hygiene-only, redundant, or not specific enough to change developer behavior. "
        "Reject any candidate that relies on unseen code, generic security folklore, or broad claims about the whole repository. "
        "If no candidate survives verification, keep `findings` empty, keep `near_misses` for the strongest rejected candidates, and make `why_no_finding` explain why the surviving workspace evidence did not clear the publication bar."
    )


def _run_two_pass_repo_audit(
    *,
    task_data: dict[str, Any],
    payload: str,
    review_max_chars: int,
    invoke_model: Any,
) -> str:
    inventory_paths = _clean_review_list(
        _repo_audit_inputs(task_data).get("inventory_paths"),
        max_items=300,
        max_chars=160,
    )
    repo_audit_lenses = _repo_audit_lenses_for_task(task_data)
    candidate_payload = _payload_with_repo_audit_lenses(payload, repo_audit_lenses)
    candidate_reply = invoke_model(
        _candidate_system_prompt_for_repo_audit(task_data, review_max_chars=review_max_chars),
        candidate_payload,
    )
    candidate_packet = _extract_repo_audit_candidate_packet(candidate_reply, allowed_paths=inventory_paths)
    candidates = candidate_packet["risk_candidates"]
    verification_payload = _candidate_payload_for_repo_audit_verification(
        payload,
        candidates,
        repo_audit_lenses=repo_audit_lenses,
        coverage=candidate_packet["coverage"],
    )
    final_reply = invoke_model(
        _verification_system_prompt_for_repo_audit(task_data, review_max_chars=review_max_chars),
        verification_payload,
    )
    rendered = _render_structured_repo_audit_with_task_data(final_reply, max_chars=review_max_chars, task_data=task_data)
    if rendered:
        return rendered
    finalized = _finalize_model_reply(final_reply, max_chars=review_max_chars)
    if finalized:
        return finalized
    return final_reply[:review_max_chars].rstrip() or "[repo_audit runtime] review response was empty"


_WEAK_REVIEW_PATTERNS = [
    r"\bmissing context\b",
    r"\bpatch excerpt\b",
    r"\btruncated\b",
    r"\bcannot verify\b",
    r"\bimpossible to verify\b",
    r"\bwithout seeing\b",
    r"\bwithout the full\b",
    r"\bdoes not show\b",
    r"\bdoesn't show\b",
    r"\bnot enough context\b",
    r"\bwe need to\b",
    r"\bwe should\b",
]


_GENERIC_NO_FINDING_PATTERNS = [
    r"\blooks[- ]good\b",
    r"\blooks[- ]correct\b",
    r"\bwell-structured\b",
    r"\bwell structured\b",
    r"\bwell-scoped\b",
    r"\bwell scoped\b",
    r"\bwell-contained\b",
    r"\bwell contained\b",
    r"\bclean change\b",
    r"\bclean patch\b",
    r"\blgtm\b",
    r"\bno issues found\b",
    r"\bno problems found\b",
]


def _is_weak_review_text(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    return any(re.search(pattern, lowered) for pattern in _WEAK_REVIEW_PATTERNS)


def _is_generic_no_finding_text(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    return any(re.search(pattern, lowered) for pattern in _GENERIC_NO_FINDING_PATTERNS)


def _default_review_risk_areas(task_data: Optional[dict[str, Any]]) -> list[str]:
    lenses = _review_lenses_for_task(task_data or {})
    cleaned: list[str] = []
    for lens in lenses:
        text = _clean_review_label(lens, max_chars=100)
        if not text:
            continue
        if text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= 4:
            break
    return cleaned


def _default_review_checks(
    *,
    touched_paths: list[str],
    tests_reviewed: list[str],
    verified_identifiers: list[str],
    task_data: Optional[dict[str, Any]],
) -> list[str]:
    checks: list[str] = []
    if touched_paths:
        rendered_paths = ", ".join(f"`{path}`" for path in touched_paths[:3])
        checks.append(f"reviewed the changed diff for {rendered_paths}")
    if verified_identifiers:
        rendered_ids = ", ".join(f"`{name}`" for name in verified_identifiers[:3])
        checks.append(f"verified changed identifiers {rendered_ids} against the supplied review context")
    if tests_reviewed:
        rendered_tests = ", ".join(f"`{path}`" for path in tests_reviewed[:2])
        checks.append(f"checked relevant changed tests {rendered_tests}")
    github_inputs = _review_github_inputs(task_data or {})
    ci_checks = github_inputs.get("ci_checks")
    if isinstance(ci_checks, dict) and ci_checks.get("has_checks"):
        state = _clean_review_label(ci_checks.get("state"), max_chars=40)
        if state:
            checks.append(f"noted GitHub checks were `{state}` at review time")
    return checks[:4]


def _default_no_finding_reason(
    *,
    touched_paths: list[str],
    verified_identifiers: list[str],
    tests_reviewed: list[str],
) -> str:
    evidence_bits: list[str] = []
    if verified_identifiers:
        evidence_bits.append(", ".join(f"`{name}`" for name in verified_identifiers[:3]))
    elif touched_paths:
        evidence_bits.append(", ".join(f"`{path}`" for path in touched_paths[:2]))
    if tests_reviewed:
        evidence_bits.append(", ".join(f"`{path}`" for path in tests_reviewed[:2]))
    if evidence_bits:
        return (
            "No concrete correctness, regression, or missing-validation issue was supported after checking "
            + " and ".join(evidence_bits)
            + "."
        )
    return "No concrete correctness, regression, or missing-validation issue was directly supported by the supplied diff."


def _default_review_summary(
    *,
    touched_paths: list[str],
    verified_identifiers: list[str],
) -> str:
    if verified_identifiers:
        rendered_ids = ", ".join(f"`{name}`" for name in verified_identifiers[:3])
        return f"Reviewed the changed behavior around {rendered_ids} and did not find a concrete issue supported by the diff."
    if touched_paths:
        rendered_paths = ", ".join(f"`{path}`" for path in touched_paths[:3])
        return f"Reviewed the changed diff for {rendered_paths} and did not find a concrete issue supported by the supplied patch."
    return "Reviewed the provided diff context and did not identify a concrete issue directly supported by the supplied patch excerpts."


def _default_review_observation(
    *,
    touched_paths: list[str],
    verified_identifiers: list[str],
    tests_reviewed: list[str],
) -> str:
    if verified_identifiers and touched_paths:
        rendered_ids = ", ".join(f"`{name}`" for name in verified_identifiers[:2])
        rendered_path = f"`{touched_paths[0]}`"
        if tests_reviewed:
            rendered_test = f"`{tests_reviewed[0]}`"
            return (
                f"{rendered_ids} stay scoped to {rendered_path}, and the changed test context in {rendered_test} "
                "supports the reviewed low-risk path."
            )
        return f"{rendered_ids} stay scoped to {rendered_path} in the reviewed diff, so the change looks contained."
    if touched_paths:
        rendered_path = f"`{touched_paths[0]}`"
        if tests_reviewed:
            rendered_test = f"`{tests_reviewed[0]}`"
            return f"The reviewed diff in {rendered_path} stays consistent with the changed test coverage in {rendered_test}."
        return f"The reviewed diff in {rendered_path} stays scoped and does not show a concrete regression trigger."
    return ""


def _render_default_structured_review(
    *,
    task_data: Optional[dict[str, Any]],
    max_chars: int,
    summary_hint: str = "",
) -> str:
    github_inputs = _review_github_inputs(task_data or {})
    touched_paths = _clean_review_list(github_inputs.get("touched_paths"), max_items=4, max_chars=120)
    tests_reviewed = _clean_review_list(github_inputs.get("touched_tests"), max_items=3, max_chars=120)
    risk_pack = github_inputs.get("risk_pack")
    verified_identifiers = (
        _clean_review_list(risk_pack.get("changed_identifiers"), max_items=3, max_chars=80)
        if isinstance(risk_pack, dict)
        else []
    )
    risk_areas_checked = _default_review_risk_areas(task_data)
    checks_performed = _default_review_checks(
        touched_paths=touched_paths,
        tests_reviewed=tests_reviewed,
        verified_identifiers=verified_identifiers,
        task_data=task_data,
    )
    summary = _clean_review_text(summary_hint, max_chars=500)
    if _is_weak_review_text(summary):
        summary = ""
    if not summary:
        summary = _default_review_summary(
            touched_paths=touched_paths,
            verified_identifiers=verified_identifiers,
        )
    observation = _default_review_observation(
        touched_paths=touched_paths,
        verified_identifiers=verified_identifiers,
        tests_reviewed=tests_reviewed,
    )
    sections = ["## Review Summary", summary]
    if observation:
        sections.append(f"Observation: {observation}")
    if touched_paths:
        sections.append("Touched paths reviewed: " + ", ".join(f"`{path}`" for path in touched_paths))
    if tests_reviewed:
        sections.append("Tests reviewed: " + ", ".join(f"`{path}`" for path in tests_reviewed))
    if risk_areas_checked:
        sections.append("Risk areas checked: " + ", ".join(risk_areas_checked))
    if checks_performed:
        sections.append("Checks performed: " + ", ".join(checks_performed))
    if verified_identifiers:
        sections.append("Changed identifiers verified: " + ", ".join(f"`{name}`" for name in verified_identifiers))
    return _finalize_model_reply("\n\n".join(sections), max_chars=max_chars)


def _finalize_model_reply(text: str, *, max_chars: int) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("\r\n", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > max_chars:
        clipped = cleaned[:max_chars].rstrip()
        breakpoints = [
            clipped.rfind("\n\n"),
            clipped.rfind("\n- "),
            clipped.rfind(". "),
            clipped.rfind("! "),
            clipped.rfind("? "),
        ]
        best_break = max(breakpoints)
        if best_break >= max(120, max_chars // 2):
            if clipped[best_break : best_break + 2] in {". ", "! ", "? "}:
                clipped = clipped[: best_break + 1].rstrip()
            else:
                clipped = clipped[:best_break].rstrip()
        cleaned = clipped
    if cleaned and cleaned[-1] not in ".!?":
        last_sentence = max(cleaned.rfind(". "), cleaned.rfind("! "), cleaned.rfind("? "))
        if last_sentence >= max(80, len(cleaned) // 2):
            cleaned = cleaned[: last_sentence + 1].rstrip()
        if cleaned and cleaned[-1] not in ".!?":
            cleaned += "."
    return cleaned


def _render_structured_review(text: str, *, max_chars: int) -> str:
    return _render_structured_review_with_task_data(text, max_chars=max_chars, task_data=None)


def _default_repo_audit_summary(
    *,
    repo_url: str,
    ref: str,
    inventory_paths: list[str],
) -> str:
    scope = repo_url or "the supplied repository"
    if ref:
        scope = f"{scope} @ {ref}"
    if inventory_paths:
        return f"Audited {scope} against the checked-out local workspace inventory and did not verify a concrete issue from the supplied evidence."
    return f"Could not verify a concrete repository finding for {scope} because no authoritative workspace inventory was supplied."


def _default_repo_audit_checks(
    *,
    repo_url: str,
    ref: str,
    inventory_paths: list[str],
) -> list[str]:
    checks: list[str] = []
    if repo_url:
        if ref:
            checks.append(f"checked out `{repo_url}` at ref `{ref}`")
        else:
            checks.append(f"checked out `{repo_url}` in a local audit workspace")
    if inventory_paths:
        sample = ", ".join(f"`{path}`" for path in inventory_paths[:3])
        checks.append(f"verified findings only against the supplied tracked-file inventory, including {sample}")
    else:
        checks.append("refused to publish findings without an authoritative tracked-file inventory")
    return checks[:4]


def _default_repo_audit_risk_areas(task_data: Optional[dict[str, Any]]) -> list[str]:
    lenses = _repo_audit_lenses_for_task(task_data or {})
    cleaned: list[str] = []
    for lens in lenses:
        text = _clean_review_label(lens, max_chars=100)
        if not text:
            continue
        if text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= 5:
            break
    return cleaned


def _default_repo_audit_coverage_summary(
    *,
    repo_url: str,
    ref: str,
    inventory_paths: list[str],
    files_deep_read: Optional[list[str]] = None,
    areas_not_deeply_reviewed: Optional[list[str]] = None,
) -> str:
    scope = repo_url or "the supplied repository"
    if ref:
        scope = f"{scope} @ {ref}"
    deep_read = files_deep_read or []
    shallow = areas_not_deeply_reviewed or []
    coverage_bits: list[str] = []
    if deep_read:
        coverage_bits.append("Deep read: " + ", ".join(f"`{path}`" for path in deep_read[:6]))
    if shallow:
        coverage_bits.append("Not deeply reviewed: " + ", ".join(shallow[:4]))
    if coverage_bits:
        prefix = (
            f"Checked the local workspace for {scope} against {len(inventory_paths)} tracked files."
            if inventory_paths
            else f"Checked the local workspace for {scope}."
        )
        return prefix + " " + " ".join(coverage_bits)
    if inventory_paths:
        return (
            f"Checked the local workspace for {scope} against {len(inventory_paths)} tracked files and only "
            "published findings that stayed grounded in that supplied inventory and selected file contents."
        )
    return f"Coverage stayed fail-closed for {scope} because no authoritative local workspace inventory was available."


def _normalize_repo_audit_severity(value: Any) -> str:
    severity = _clean_review_label(value, max_chars=20).lower()
    if severity in {"critical", "high"}:
        return "high"
    if severity in {"medium", "moderate"}:
        return "medium"
    if severity in {"low", "minor", "info", "informational"}:
        return "low"
    return "medium"


def _normalize_repo_audit_confidence(value: Any) -> str:
    confidence = _clean_review_label(value, max_chars=20).lower()
    if confidence in {"high", "strong"}:
        return "high"
    if confidence in {"medium", "moderate"}:
        return "medium"
    if confidence in {"low", "weak"}:
        return "low"
    return "medium"


def _normalize_repo_audit_fix_priority(value: Any) -> str:
    priority = _clean_review_label(value, max_chars=24).lower()
    if priority in {"fix_now", "now", "immediate"}:
        return "fix_now"
    if priority in {"next_change", "next-change", "next"}:
        return "next_change"
    if priority in {"later", "backlog"}:
        return "later"
    return "next_change"


def _normalize_repo_audit_proof_type(value: Any) -> str:
    proof_type = _clean_review_label(value, max_chars=40).lower()
    allowed = {
        "code_path",
        "cross_file_interaction",
        "test_gap",
        "config_deploy_mismatch",
        "inventory_grounding",
    }
    return proof_type if proof_type in allowed else "code_path"


def _repo_audit_finding_sort_key(finding: dict[str, str]) -> tuple[int, int, str]:
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    priority_rank = {"fix_now": 0, "next_change": 1, "later": 2}
    return (
        severity_rank.get(finding.get("severity", "medium"), 1),
        confidence_rank.get(finding.get("confidence", "medium"), 1),
        priority_rank.get(finding.get("fix_priority", "next_change"), 1),
        finding.get("file", ""),
    )


def _repo_audit_supporting_files(
    raw_values: Any,
    *,
    allowed_path_keys: dict[str, str],
) -> list[str]:
    supporting_files_raw = _clean_review_list(raw_values, max_items=6, max_chars=160)
    supporting_files: list[str] = []
    seen: set[str] = set()
    for item in supporting_files_raw:
        matched = allowed_path_keys.get(item.lower())
        if not matched:
            continue
        lowered = matched.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        supporting_files.append(matched)
    return supporting_files


def _render_default_repo_audit(
    *,
    task_data: Optional[dict[str, Any]],
    max_chars: int,
    summary_hint: str = "",
) -> str:
    repo_audit = _repo_audit_inputs(task_data or {})
    repo_url = _clean_review_label(repo_audit.get("repo_url"), max_chars=200)
    ref = _clean_review_label(repo_audit.get("ref"), max_chars=120)
    inventory_paths = _clean_review_list(repo_audit.get("inventory_paths"), max_items=300, max_chars=160)
    coverage_summary = _default_repo_audit_coverage_summary(
        repo_url=repo_url,
        ref=ref,
        inventory_paths=inventory_paths,
    )
    summary = _clean_review_text(summary_hint, max_chars=500)
    if _is_weak_review_text(summary):
        summary = ""
    if not summary:
        summary = _default_repo_audit_summary(repo_url=repo_url, ref=ref, inventory_paths=inventory_paths)
    checks_performed = _default_repo_audit_checks(repo_url=repo_url, ref=ref, inventory_paths=inventory_paths)
    risk_areas_checked = _default_repo_audit_risk_areas(task_data)
    why_no_finding = (
        "No published finding survived verification against the checked-out workspace inventory and supplied file excerpts."
        if inventory_paths
        else "No authoritative local workspace inventory was available, so findings were withheld instead of speculating."
    )
    sections = ["## Repo Audit Summary", summary]
    if repo_url:
        scope = f"`{repo_url}`"
        if ref:
            scope += f" @ `{ref}`"
        sections.append(f"Repository scope: {scope}")
    if inventory_paths:
        sections.append("Tracked files verified: " + ", ".join(f"`{path}`" for path in inventory_paths[:8]))
    sections.append(f"Coverage summary: {coverage_summary}")
    if risk_areas_checked:
        sections.append("Risk areas checked: " + ", ".join(risk_areas_checked))
    if checks_performed:
        sections.append("Checks performed: " + ", ".join(checks_performed))
    sections.append(f"Why no finding: {why_no_finding}")
    return _finalize_model_reply("\n\n".join(sections), max_chars=max_chars)


def _render_structured_repo_audit_with_task_data(
    text: str,
    *,
    max_chars: int,
    task_data: Optional[dict[str, Any]],
) -> str:
    parsed = _extract_first_json_object(text)
    if not isinstance(parsed, dict):
        if _is_adapter_error(text):
            return ""
        return _render_default_repo_audit(task_data=task_data, max_chars=max_chars, summary_hint=text)
    if _is_adapter_error(text):
        return ""
    repo_audit = _repo_audit_inputs(task_data or {})
    repo_url = _clean_review_label(repo_audit.get("repo_url"), max_chars=200)
    ref = _clean_review_label(repo_audit.get("ref"), max_chars=120)
    inventory_paths = _clean_review_list(repo_audit.get("inventory_paths"), max_items=300, max_chars=160)
    allowed_path_keys = {item.lower(): item for item in inventory_paths}
    coverage_summary = _clean_review_text(parsed.get("coverage_summary"), max_chars=420)
    if _is_weak_review_text(coverage_summary):
        coverage_summary = ""
    files_deep_read_raw = _clean_review_list(parsed.get("files_deep_read"), max_items=12, max_chars=160)
    files_deep_read = [allowed_path_keys[item.lower()] for item in files_deep_read_raw if item.lower() in allowed_path_keys]
    areas_not_deeply_reviewed = _clean_review_list(parsed.get("areas_not_deeply_reviewed"), max_items=6, max_chars=120)
    summary = _clean_review_text(parsed.get("summary"), max_chars=500)
    if _is_weak_review_text(summary):
        summary = ""
    repo_overview = _clean_review_text(parsed.get("repo_overview"), max_chars=500)
    if _is_weak_review_text(repo_overview):
        repo_overview = ""
    risk_areas_checked = _clean_review_list(parsed.get("risk_areas_checked"), max_items=5, max_chars=100)
    checks_performed = _clean_review_list(parsed.get("checks_performed"), max_items=5, max_chars=140)
    why_no_finding = _clean_review_text(parsed.get("why_no_finding"), max_chars=400)
    if _is_weak_review_text(why_no_finding):
        why_no_finding = ""
    rendered_findings: list[str] = []
    rendered_near_misses: list[str] = []
    rendered_observations: list[str] = []
    findings_for_sort: list[dict[str, str]] = []
    findings_raw = parsed.get("findings")
    if isinstance(findings_raw, list):
        for item in findings_raw[:5]:
            if not isinstance(item, dict):
                continue
            file_name = _clean_review_label(item.get("file"), max_chars=160)
            matched = allowed_path_keys.get(file_name.lower()) if file_name else None
            if not matched:
                continue
            title = _clean_review_text(item.get("title") or item.get("issue"), max_chars=180)
            category = _clean_review_label(item.get("category"), max_chars=60)
            severity = _normalize_repo_audit_severity(item.get("severity"))
            confidence = _normalize_repo_audit_confidence(item.get("confidence"))
            invariant = _clean_review_text(item.get("invariant"), max_chars=180)
            failure_mode = _clean_review_text(item.get("failure_mode"), max_chars=220)
            proof_type = _normalize_repo_audit_proof_type(item.get("proof_type"))
            fix_priority = _normalize_repo_audit_fix_priority(item.get("fix_priority"))
            developer_impact = _clean_review_text(
                item.get("developer_impact") or item.get("impact") or item.get("rationale"),
                max_chars=240,
            )
            evidence = _clean_review_text(item.get("evidence") or item.get("rationale"), max_chars=260)
            supporting_files = _repo_audit_supporting_files(
                item.get("supporting_files"),
                allowed_path_keys=allowed_path_keys,
            )
            same_file_check = _clean_review_text(item.get("same_file_check"), max_chars=220)
            same_file_contradicted_by = _clean_review_list(
                item.get("same_file_contradicted_by"),
                max_items=4,
                max_chars=140,
            )
            contradiction_check = _clean_review_text(item.get("contradiction_check"), max_chars=220)
            contradicted_by = _clean_review_list(item.get("contradicted_by"), max_items=4, max_chars=140)
            next_step = _clean_review_text(item.get("next_step"), max_chars=220)
            note = (
                developer_impact
                or evidence
                or next_step
                or "Grounded note from the checked-out workspace."
            )
            combined = f"{title} {invariant} {failure_mode} {developer_impact} {evidence} {next_step}".strip()
            if not title or _is_weak_review_text(f"{title} {note}".strip()):
                continue
            if severity == "low" or confidence == "low":
                rendered_observations.append(f"`{matched}`: {note}")
                continue
            if matched not in files_deep_read:
                title_for_near_miss = title.rstrip(".:; ")
                rendered_near_misses.append(
                    f"`{matched}` - {title_for_near_miss}: The claim was withheld because the file was not listed in files_deep_read."
                )
                continue
            if not invariant or not failure_mode or _is_weak_review_text(combined):
                continue
            if severity == "high":
                deep_read_supporting_files = [path for path in supporting_files if path in files_deep_read]
                distinct_supporting_files = [path for path in deep_read_supporting_files if path != matched]
                if not distinct_supporting_files:
                    title_for_near_miss = title.rstrip(".:; ")
                    rendered_near_misses.append(
                        f"`{matched}` - {title_for_near_miss}: The claim was withheld because high-severity findings must cite at least one additional deep-read supporting file."
                    )
                    continue
                if not same_file_check or _is_weak_review_text(same_file_check):
                    title_for_near_miss = title.rstrip(".:; ")
                    rendered_near_misses.append(
                        f"`{matched}` - {title_for_near_miss}: The claim was withheld because high-severity findings must describe the same-file contradiction check that ruled out a nearby guard or branch."
                    )
                    continue
                if same_file_contradicted_by:
                    title_for_near_miss = title.rstrip(".:; ")
                    rendered_near_misses.append(
                        f"`{matched}` - {title_for_near_miss}: The claim was withheld because same-file contradictory evidence remained ({'; '.join(same_file_contradicted_by[:2])})."
                    )
                    continue
                if not contradiction_check or _is_weak_review_text(contradiction_check):
                    title_for_near_miss = title.rstrip(".:; ")
                    rendered_near_misses.append(
                        f"`{matched}` - {title_for_near_miss}: The claim was withheld because high-severity findings must describe the contradiction check that ruled out an enforcing caller or guard."
                    )
                    continue
                if contradicted_by:
                    title_for_near_miss = title.rstrip(".:; ")
                    rendered_near_misses.append(
                        f"`{matched}` - {title_for_near_miss}: The claim was withheld because contradictory workspace evidence remained ({'; '.join(contradicted_by[:2])})."
                    )
                    continue
            finding_bits = [f"**[{severity}/{confidence}/{fix_priority}] {title}** (`{matched}`)"]
            if category:
                finding_bits.append(f"Category: {category}.")
            finding_bits.append(f"Invariant: {invariant}")
            finding_bits.append(f"Failure mode: {failure_mode}")
            finding_bits.append(f"Proof: {proof_type}")
            if developer_impact:
                finding_bits.append(f"Developer impact: {developer_impact}")
            if evidence:
                finding_bits.append(f"Evidence: {evidence}")
            if supporting_files:
                finding_bits.append(
                    "Supporting files: "
                    + ", ".join(f"`{path}`" for path in supporting_files[:4])
                )
            if same_file_check:
                finding_bits.append(f"Same-file check: {same_file_check}")
            if contradiction_check:
                finding_bits.append(f"Contradiction check: {contradiction_check}")
            if next_step:
                finding_bits.append(f"Next step: {next_step}")
            findings_for_sort.append(
                {
                    "severity": severity,
                    "confidence": confidence,
                    "fix_priority": fix_priority,
                    "file": matched,
                    "rendered": " ".join(finding_bits).strip(),
                }
            )
    findings_for_sort.sort(key=_repo_audit_finding_sort_key)
    rendered_findings.extend(entry["rendered"] for entry in findings_for_sort[:5])
    near_misses_raw = parsed.get("near_misses")
    if isinstance(near_misses_raw, list):
        for item in near_misses_raw[:4]:
            if not isinstance(item, dict):
                continue
            file_name = _clean_review_label(item.get("file"), max_chars=160)
            matched = allowed_path_keys.get(file_name.lower()) if file_name else None
            if not matched:
                continue
            title = _clean_review_text(item.get("title"), max_chars=160).rstrip(".:; ")
            reason = _clean_review_text(item.get("reason_not_published"), max_chars=220)
            if not title or not reason or _is_weak_review_text(f"{title} {reason}"):
                continue
            rendered_near_misses.append(f"`{matched}` - {title}: {reason}")
    observations_raw = parsed.get("observations")
    if isinstance(observations_raw, list):
        for item in observations_raw[:4]:
            if not isinstance(item, dict):
                continue
            file_name = _clean_review_label(item.get("file"), max_chars=160)
            matched = allowed_path_keys.get(file_name.lower()) if file_name else None
            if not matched:
                continue
            note = _clean_review_text(item.get("note"), max_chars=260)
            if not note or _is_weak_review_text(note):
                continue
            rendered_observations.append(f"`{matched}`: {note}")
    if not summary:
        summary = _default_repo_audit_summary(repo_url=repo_url, ref=ref, inventory_paths=inventory_paths)
    if not coverage_summary:
        coverage_summary = _default_repo_audit_coverage_summary(
            repo_url=repo_url,
            ref=ref,
            inventory_paths=inventory_paths,
            files_deep_read=files_deep_read,
            areas_not_deeply_reviewed=areas_not_deeply_reviewed,
        )
    if not checks_performed:
        checks_performed = _default_repo_audit_checks(repo_url=repo_url, ref=ref, inventory_paths=inventory_paths)
    if not rendered_findings and not why_no_finding:
        why_no_finding = (
            "No top finding remained after filtering to grounded, invariant-backed claims with concrete developer impact."
            if inventory_paths
            else "No authoritative tracked-file inventory was available, so findings were withheld."
        )
    sections = ["## Repo Audit Findings" if rendered_findings else "## Repo Audit Summary", summary]
    if repo_url:
        scope = f"`{repo_url}`"
        if ref:
            scope += f" @ `{ref}`"
        sections.append(f"Repository scope: {scope}")
    if repo_overview:
        sections.append(f"Repository overview: {repo_overview}")
    sections.append(f"Coverage summary: {coverage_summary}")
    if files_deep_read:
        sections.append("Files deep read: " + ", ".join(f"`{path}`" for path in files_deep_read[:6]))
    if areas_not_deeply_reviewed:
        sections.append("Areas not deeply reviewed: " + ", ".join(areas_not_deeply_reviewed[:4]))
    if inventory_paths:
        sections.append("Tracked files verified: " + ", ".join(f"`{path}`" for path in inventory_paths[:8]))
    if risk_areas_checked:
        sections.append("Risk areas checked: " + ", ".join(risk_areas_checked))
    if checks_performed:
        sections.append("Checks performed: " + ", ".join(checks_performed))
    if why_no_finding:
        sections.append(f"Why no finding: {why_no_finding}")
    for index, finding in enumerate(rendered_findings, start=1):
        sections.append(f"{index}. {finding}")
    if rendered_near_misses:
        sections.append("Near misses: " + " | ".join(rendered_near_misses[:3]))
    if rendered_observations:
        sections.append("Observations: " + " | ".join(rendered_observations[:3]))
    return _finalize_model_reply("\n\n".join(sections), max_chars=max_chars)


def _render_structured_review_with_task_data(
    text: str,
    *,
    max_chars: int,
    task_data: Optional[dict[str, Any]],
) -> str:
    if _task_requires_repo_audit_prompt(task_data or {}):
        return _render_structured_repo_audit_with_task_data(text, max_chars=max_chars, task_data=task_data)
    parsed = _extract_first_json_object(text)
    approval_mode = _task_is_approval_review(task_data or {})
    if not isinstance(parsed, dict):
        if _is_adapter_error(text) or approval_mode:
            return ""
        return _render_default_structured_review(task_data=task_data, max_chars=max_chars, summary_hint=text)
    if _is_adapter_error(text):
        return ""
    github_inputs = _review_github_inputs(task_data or {})
    allowed_paths = _clean_review_list(github_inputs.get("touched_paths"), max_items=4, max_chars=120)
    allowed_tests = _clean_review_list(github_inputs.get("touched_tests"), max_items=3, max_chars=120)
    risk_pack = github_inputs.get("risk_pack")
    allowed_identifiers = (
        _clean_review_list(
            risk_pack.get("changed_identifiers"),
            max_items=8,
            max_chars=80,
        )
        if isinstance(risk_pack, dict)
        else []
    )
    summary = _clean_review_text(parsed.get("summary"), max_chars=500)
    if _is_weak_review_text(summary):
        summary = ""
    observation = _clean_review_text(parsed.get("observation"), max_chars=400)
    if _is_weak_review_text(observation):
        observation = ""
    touched_paths = _clean_review_list(parsed.get("touched_paths"), max_items=4, max_chars=120)
    touched_paths = _filter_review_list_to_allowed(touched_paths, allowed_paths)
    if not touched_paths:
        touched_paths = allowed_paths
    tests_reviewed = _clean_review_list(parsed.get("tests_reviewed"), max_items=3, max_chars=120)
    tests_reviewed = _filter_review_list_to_allowed(tests_reviewed, allowed_tests)
    if not tests_reviewed:
        tests_reviewed = allowed_tests
    risk_areas_checked = _clean_review_list(parsed.get("risk_areas_checked"), max_items=4, max_chars=100)
    checks_performed = _clean_review_list(parsed.get("checks_performed"), max_items=4, max_chars=120)
    verified_identifiers = _clean_review_list(parsed.get("verified_identifiers"), max_items=4, max_chars=80)
    verified_identifiers = _filter_review_list_to_allowed(verified_identifiers, allowed_identifiers)
    legacy_no_finding = _clean_review_text(parsed.get("why_no_finding"), max_chars=400)
    if _is_weak_review_text(legacy_no_finding):
        legacy_no_finding = ""
    findings_raw = parsed.get("findings")
    findings: list[str] = []
    allowed_path_keys = {item.lower(): item for item in allowed_paths}
    if isinstance(findings_raw, list):
        for item in findings_raw[:2]:
            if not isinstance(item, dict):
                continue
            issue = _clean_review_text(item.get("issue"), max_chars=200)
            if not issue:
                continue
            rationale = _clean_review_text(item.get("rationale"), max_chars=400)
            combined = f"{issue} {rationale}".strip()
            if _is_weak_review_text(combined):
                continue
            file_name = _clean_review_label(item.get("file"), max_chars=80)
            if file_name and allowed_path_keys:
                matched = allowed_path_keys.get(file_name.lower())
                if not matched:
                    continue
                file_name = matched
            if file_name:
                findings.append(f"**{issue}** (`{file_name}`): {rationale or 'Check this path.'}")
            else:
                findings.append(f"**{issue}**: {rationale or 'Check this logic.'}")
    if findings and allowed_identifiers and not verified_identifiers:
        findings = []
    if not findings:
        if _is_generic_no_finding_text(summary):
            summary = ""
        if _is_generic_no_finding_text(observation):
            observation = ""
        if not verified_identifiers:
            verified_identifiers = allowed_identifiers[:3]
        if not risk_areas_checked:
            risk_areas_checked = _default_review_risk_areas(task_data)
        if not checks_performed:
            checks_performed = _default_review_checks(
                touched_paths=touched_paths,
                tests_reviewed=tests_reviewed,
                verified_identifiers=verified_identifiers,
                task_data=task_data,
            )
        if not summary:
            summary = _default_review_summary(
                touched_paths=touched_paths,
                verified_identifiers=verified_identifiers,
            )
        if not observation and legacy_no_finding:
            observation = legacy_no_finding
        if _is_generic_no_finding_text(observation):
            observation = ""
        if not observation and (touched_paths or verified_identifiers):
            observation = _default_review_observation(
                touched_paths=touched_paths,
                verified_identifiers=verified_identifiers,
                tests_reviewed=tests_reviewed,
            )
    elif not approval_mode:
        if not summary and touched_paths:
            summary = _default_review_summary(
                touched_paths=touched_paths,
                verified_identifiers=verified_identifiers,
            )
    sections: list[str] = []
    if findings:
        sections.append("## Review Findings")
        if summary:
            sections.append(summary)
        if observation:
            sections.append(f"Observation: {observation}")
        if touched_paths:
            sections.append("Touched paths reviewed: " + ", ".join(f"`{path}`" for path in touched_paths))
        if tests_reviewed:
            sections.append("Tests reviewed: " + ", ".join(f"`{path}`" for path in tests_reviewed))
        if risk_areas_checked:
            sections.append("Risk areas checked: " + ", ".join(risk_areas_checked))
        if checks_performed:
            sections.append("Checks performed: " + ", ".join(checks_performed))
        if verified_identifiers:
            sections.append("Changed identifiers verified: " + ", ".join(f"`{name}`" for name in verified_identifiers))
        for index, finding in enumerate(findings, start=1):
            sections.append(f"{index}. {finding}")
    elif summary:
        sections.append("## Review Summary")
        sections.append(summary)
        if observation:
            sections.append(f"Observation: {observation}")
        if touched_paths:
            sections.append("Touched paths reviewed: " + ", ".join(f"`{path}`" for path in touched_paths))
        if tests_reviewed:
            sections.append("Tests reviewed: " + ", ".join(f"`{path}`" for path in tests_reviewed))
        if risk_areas_checked:
            sections.append("Risk areas checked: " + ", ".join(risk_areas_checked))
        if checks_performed:
            sections.append("Checks performed: " + ", ".join(checks_performed))
        if verified_identifiers:
            sections.append("Changed identifiers verified: " + ", ".join(f"`{name}`" for name in verified_identifiers))
    else:
        sections.append("## Review Summary")
        sections.append(
            "Reviewed the provided diff context and did not identify a concrete issue that is directly supported by the supplied patch excerpts."
        )
        if touched_paths:
            sections.append("Touched paths reviewed: " + ", ".join(f"`{path}`" for path in touched_paths))
        if tests_reviewed:
            sections.append("Tests reviewed: " + ", ".join(f"`{path}`" for path in tests_reviewed))
        if risk_areas_checked:
            sections.append("Risk areas checked: " + ", ".join(risk_areas_checked))
        if checks_performed:
            sections.append("Checks performed: " + ", ".join(checks_performed))
        if verified_identifiers:
            sections.append("Changed identifiers verified: " + ", ".join(f"`{name}`" for name in verified_identifiers))
    rendered = "\n\n".join(section for section in sections if section.strip())
    return _finalize_model_reply(rendered, max_chars=max_chars)


@dataclass
class AIAdapter:
    """Real AI adapter using Ollama for provider task processing."""

    model: str = "tinyllama"

    def generate_reply(self, payload: str, task_data: dict[str, Any]) -> str:
        import subprocess

        try:
            review_max = _review_max_chars()
            if _task_requires_repo_audit_prompt(task_data):
                def _invoke(system_prompt: str, user_payload: str) -> str:
                    prompt = f"{system_prompt}\n\nTask: {user_payload}\n\nReply:"
                    result = subprocess.run(
                        ["ollama", "run", self.model, prompt],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    return (result.stdout or "").strip()

                result = _run_two_pass_repo_audit(
                    task_data=task_data,
                    payload=payload,
                    review_max_chars=review_max,
                    invoke_model=_invoke,
                )
                if result:
                    return result
                return f"[AI adapter] empty response from {self.model}"
            if _task_requires_review_prompt(task_data):
                def _invoke(system_prompt: str, user_payload: str) -> str:
                    prompt = f"{system_prompt}\n\nTask: {user_payload}\n\nReply:"
                    result = subprocess.run(
                        ["ollama", "run", self.model, prompt],
                        capture_output=True,
                        text=True,
                        timeout=45,
                    )
                    return (result.stdout or "").strip()

                result = _run_two_pass_review(
                    task_data=task_data,
                    payload=payload,
                    review_max_chars=review_max,
                    invoke_model=_invoke,
                )
                if result:
                    return result
                return f"[AI adapter] empty response from {self.model}"
            prompt = (
                f"{_system_prompt_for_task(task_data, generic_max_chars=300, review_max_chars=review_max)}\n\n"
                f"Task: {payload}\n\nReply:"
            )
            result = subprocess.run(
                ["ollama", "run", self.model, prompt],
                capture_output=True,
                text=True,
                timeout=45,
            )
            reply = (result.stdout or "").strip()
            if not reply:
                return f"[AI adapter] empty response from {self.model}"
            return reply
        except subprocess.TimeoutExpired:
            return f"[AI adapter] {self.model} timed out"
        except Exception as exc:  # noqa: BLE001
            return f"[AI adapter] error: {exc}"


@dataclass
class DeepSeekAdapter:
    """Real AI adapter using DeepSeek API for provider task processing."""

    api_key: str = ""
    model: str = "deepseek-chat"

    def generate_reply(self, payload: str, task_data: dict[str, Any]) -> str:
        review_max = _review_max_chars()
        try:
            def _invoke(system_prompt: str, user_payload: str) -> str:
                resp = requests.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_payload},
                        ],
                        "max_tokens": _env_positive_int("MEP_AI_MAX_TOKENS", 4000),
                        "temperature": 0.1,
                    },
                    timeout=_env_positive_int("MEP_AI_TIMEOUT_SECONDS", 120),
                )
                if resp.status_code != 200:
                    return f"[DeepSeek] API error {resp.status_code}: {resp.text[:200]}"
                message = resp.json()["choices"][0]["message"]
                reply = str(message.get("content") or "").strip()
                if not reply:
                    reply = str(message.get("reasoning_content") or "").strip()
                return reply

            if _task_requires_repo_audit_prompt(task_data):
                result = _run_two_pass_repo_audit(
                    task_data=task_data,
                    payload=payload,
                    review_max_chars=review_max,
                    invoke_model=_invoke,
                )
                if result:
                    return result
                return "[DeepSeek] review response was empty"

            if _task_requires_review_prompt(task_data):
                result = _run_two_pass_review(
                    task_data=task_data,
                    payload=payload,
                    review_max_chars=review_max,
                    invoke_model=_invoke,
                )
                if result:
                    return result
                return "[DeepSeek] review response was empty"

            reply = _invoke(
                _system_prompt_for_task(
                    task_data,
                    generic_max_chars=500,
                    review_max_chars=review_max,
                ),
                payload,
            )
            if reply:
                return reply
            return "[DeepSeek] review response was empty"
        except Exception as exc:  # noqa: BLE001
            return f"[DeepSeek] error: {exc}"


@dataclass
class OpenAICompatibleAdapter:
    """Real AI adapter using an OpenAI-compatible chat completions endpoint."""

    api_key: str = ""
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    provider_name: str = "openai-compatible"

    def _endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def generate_reply(self, payload: str, task_data: dict[str, Any]) -> str:
        review_max = _review_max_chars()
        try:
            def _invoke(system_prompt: str, user_payload: str) -> str:
                resp = requests.post(
                    self._endpoint(),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_payload},
                        ],
                        "max_tokens": _env_positive_int("MEP_AI_MAX_TOKENS", 4000),
                        "temperature": 0.1,
                    },
                    timeout=_env_positive_int("MEP_AI_TIMEOUT_SECONDS", 120),
                )
                if resp.status_code != 200:
                    return f"[{self.provider_name}] API error {resp.status_code}: {resp.text[:200]}"
                message = resp.json()["choices"][0]["message"]
                reply = str(message.get("content") or "").strip()
                if not reply:
                    reply = str(message.get("reasoning_content") or "").strip()
                return reply

            if _task_requires_repo_audit_prompt(task_data):
                result = _run_two_pass_repo_audit(
                    task_data=task_data,
                    payload=payload,
                    review_max_chars=review_max,
                    invoke_model=_invoke,
                )
                if result:
                    return result
                return f"[{self.provider_name}] review response was empty"

            if _task_requires_review_prompt(task_data):
                result = _run_two_pass_review(
                    task_data=task_data,
                    payload=payload,
                    review_max_chars=review_max,
                    invoke_model=_invoke,
                )
                if result:
                    return result
                return f"[{self.provider_name}] review response was empty"

            reply = _invoke(_system_prompt_for_task(task_data, generic_max_chars=300, review_max_chars=review_max), payload)
            if not reply:
                return f"[{self.provider_name}] reply was empty"
            return reply
        except requests.Timeout:
            return f"[{self.provider_name}] {self.model} timed out"
        except Exception as exc:  # noqa: BLE001
            return f"[{self.provider_name}] error: {exc}"


class WorkspaceManager:
    """Manages autonomous workspace synchronization (Git fetch/checkout)."""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _run_git(self, cwd: str, args: list[str], *, timeout_seconds: int = 60) -> tuple[int, str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            return result.returncode, (result.stdout + result.stderr).strip()
        except Exception as exc:  # noqa: BLE001
            return -1, str(exc)

    def sync_pr_workspace(self, repo_url: str, head_sha: str, head_ref: str, bridge_id: Optional[str] = None) -> tuple[bool, str]:
        """Ensures the local workspace is synced to the target PR branch/commit."""
        # Phase 4 Isolation: Use a specific directory for this bridge_id if provided
        if bridge_id:
            workspace_path = os.path.join(self.base_dir, bridge_id)
        else:
            workspace_path = os.path.join(self.base_dir, "shared")
        
        os.makedirs(workspace_path, exist_ok=True)
        
        if not os.path.exists(os.path.join(workspace_path, ".git")):
            print(f"[mep workspace] cloning {repo_url} into {workspace_path}")
            code, out = self._run_git(os.path.dirname(workspace_path), ["clone", repo_url, os.path.basename(workspace_path)])
            if code != 0:
                return False, f"clone failed: {out}"

        print(f"[mep workspace] fetching {head_ref} ({head_sha[:8]})")
        code, out = self._run_git(workspace_path, ["fetch", "origin", head_ref])
        if code != 0:
            # Fallback to fetching everything if ref-specific fetch fails
            code, out = self._run_git(workspace_path, ["fetch", "--all"])
            if code != 0:
                return False, f"fetch failed: {out}"

        print(f"[mep workspace] checking out {head_sha[:8]}")
        code, out = self._run_git(workspace_path, ["checkout", head_sha])
        if code != 0:
            return False, f"checkout failed: {out}"

        return True, workspace_path

    @staticmethod
    def _normalize_repo_clone_url(repo_url: str) -> str:
        cleaned = str(repo_url or "").strip()
        if not cleaned:
            return ""
        if "://" in cleaned or cleaned.startswith("git@"):
            return cleaned
        if cleaned.startswith("github.com/") or cleaned.startswith("www.github.com/"):
            normalized = cleaned.removeprefix("www.")
            if not normalized.startswith("github.com/"):
                normalized = normalized.split("/", 1)[-1]
                normalized = f"github.com/{normalized}"
            if not normalized.endswith(".git"):
                normalized += ".git"
            return f"https://{normalized}"
        return cleaned

    @staticmethod
    def _workspace_slug(repo_url: str) -> str:
        cleaned = str(repo_url or "").strip().lower()
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        cleaned = re.sub(r"^[a-z]+://", "", cleaned)
        cleaned = cleaned.replace("git@", "")
        cleaned = cleaned.replace(":", "/")
        cleaned = re.sub(r"[^a-z0-9._/-]+", "-", cleaned)
        cleaned = cleaned.strip("/").replace("/", "__")
        return cleaned or "repo_audit"

    def sync_repo_audit_workspace(self, repo_url: str, ref: Optional[str] = None) -> tuple[bool, str]:
        normalized_repo_url = self._normalize_repo_clone_url(repo_url)
        if not normalized_repo_url:
            return False, "repo_url is empty"
        git_timeout_seconds = _env_positive_int("MEP_REPO_AUDIT_GIT_TIMEOUT_SECONDS", 180)
        target_ref = str(ref or "").strip()
        fetch_ref = target_ref.removeprefix("origin/") if target_ref.startswith("origin/") else target_ref
        workspace_path = os.path.join(self.base_dir, "repo-audit", self._workspace_slug(normalized_repo_url))
        os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
        if not os.path.exists(os.path.join(workspace_path, ".git")):
            print(f"[mep repo_audit] cloning {normalized_repo_url} into {workspace_path}")
            clone_args = ["clone", "--no-tags"]
            if fetch_ref:
                clone_args.extend(["--single-branch", "--branch", fetch_ref])
            clone_args.extend([normalized_repo_url, os.path.basename(workspace_path)])
            code, out = self._run_git(
                os.path.dirname(workspace_path),
                clone_args,
                timeout_seconds=git_timeout_seconds,
            )
            if code != 0:
                if fetch_ref:
                    fallback_clone_args = ["clone", "--no-tags", normalized_repo_url, os.path.basename(workspace_path)]
                    code, out = self._run_git(
                        os.path.dirname(workspace_path),
                        fallback_clone_args,
                        timeout_seconds=git_timeout_seconds,
                    )
                if code != 0:
                    return False, f"clone failed: {out}"
        fetch_plans: list[list[str]] = []
        if fetch_ref:
            fetch_plans.append(["fetch", "--no-tags", "origin", fetch_ref])
        fetch_plans.append(["fetch", "--no-tags", "origin"])
        last_fetch_error = ""
        for fetch_args in fetch_plans:
            print(f"[mep repo_audit] fetching workspace for {normalized_repo_url} with {' '.join(fetch_args[1:])}")
            code, out = self._run_git(workspace_path, fetch_args, timeout_seconds=git_timeout_seconds)
            if code == 0:
                last_fetch_error = ""
                break
            last_fetch_error = out
        if last_fetch_error:
            return False, f"fetch failed: {last_fetch_error}"
        checkout_candidates = ["FETCH_HEAD"] if target_ref else []
        if target_ref:
            checkout_candidates.append(target_ref)
        if target_ref and not target_ref.startswith("origin/"):
            checkout_candidates.append(f"origin/{target_ref}")
        if not checkout_candidates:
            checkout_candidates = ["origin/HEAD"]
        last_error = ""
        for candidate in checkout_candidates:
            code, out = self._run_git(
                workspace_path,
                ["checkout", "--force", candidate],
                timeout_seconds=git_timeout_seconds,
            )
            if code == 0:
                return True, workspace_path
            last_error = out
        return False, f"checkout failed: {last_error}"

    @staticmethod
    def _repo_audit_priority(path: str) -> tuple[int, int, str]:
        normalized = str(path or "").replace("\\", "/").strip()
        lowered = normalized.lower()
        basename = lowered.rsplit("/", 1)[-1]
        if basename in {"readme.md", "readme"}:
            return (0, len(normalized), normalized)
        if basename in {"pyproject.toml", "package.json", "requirements.txt", "dockerfile"}:
            return (1, len(normalized), normalized)
        if lowered.startswith(".github/workflows/"):
            return (2, len(normalized), normalized)
        if lowered.startswith(("bridge/", "node/", "hub/", "clients/", "tests/")):
            return (3, len(normalized), normalized)
        depth = lowered.count("/")
        return (4 + min(depth, 5), len(normalized), normalized)

    @staticmethod
    def _is_repo_audit_text_path(path: str) -> bool:
        lowered = str(path or "").lower()
        text_suffixes = (
            ".md",
            ".txt",
            ".py",
            ".toml",
            ".json",
            ".yml",
            ".yaml",
            ".ini",
            ".cfg",
            ".sh",
            ".ps1",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".sql",
        )
        return lowered.endswith(text_suffixes) or "/" not in lowered

    def build_repo_audit_context(
        self,
        workspace_path: str,
        *,
        max_inventory_paths: int = 300,
        max_files: int = 10,
        max_file_chars: int = 6000,
        max_chars: int = 50000,
    ) -> tuple[str, list[str]]:
        if not workspace_path or not os.path.isdir(workspace_path):
            return "", []
        inventory: list[str] = []
        code, out = self._run_git(workspace_path, ["ls-files"])
        if code == 0:
            inventory = [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]
        if not inventory:
            for root, _dirs, files in os.walk(workspace_path):
                if ".git" in root.split(os.sep):
                    continue
                for name in files:
                    absolute = os.path.join(root, name)
                    relative = os.path.relpath(absolute, workspace_path).replace("\\", "/")
                    inventory.append(relative)
        deduped_inventory: list[str] = []
        seen_paths: set[str] = set()
        for path in sorted(inventory, key=self._repo_audit_priority):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            deduped_inventory.append(path)
        inventory = deduped_inventory[:max_inventory_paths]
        if not inventory:
            return "", []
        sections = [
            f"Local workspace path: {workspace_path}",
            "Tracked file inventory from the checked-out repository (authoritative scope for this audit):",
            "\n".join(f"- {path}" for path in inventory),
        ]
        remaining = max_chars - sum(len(section) + 2 for section in sections)
        if remaining <= 400:
            return "\n\n".join(sections), inventory
        added = 0
        for path in inventory:
            if added >= max_files or remaining <= 400:
                break
            if not self._is_repo_audit_text_path(path):
                continue
            resolved = self._resolve_repo_file(workspace_path, path)
            if not resolved or not os.path.isfile(resolved):
                continue
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError:
                continue
            if not content.strip():
                continue
            body = content[:max_file_chars].rstrip()
            block = f"### {path}\n```\n{body}\n```"
            if len(content) > max_file_chars:
                block += "\n[note: file body truncated for input budget; rely only on visible content here]"
            if len(block) > remaining:
                continue
            if added == 0:
                sections.append("Selected authoritative file contents for the repo audit:")
            sections.append(block)
            remaining -= len(block) + 2
            added += 1
        return "\n\n".join(sections), inventory

    @staticmethod
    def _resolve_repo_file(workspace_path: str, relative_path: str) -> Optional[str]:
        normalized_relative = str(relative_path or "").replace("\\", "/").strip("/")
        if not normalized_relative:
            return None
        candidate = os.path.abspath(os.path.join(workspace_path, *normalized_relative.split("/")))
        try:
            if os.path.commonpath([os.path.abspath(workspace_path), candidate]) != os.path.abspath(workspace_path):
                return None
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = str(path or "").replace("\\", "/").strip().lower()
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
    def _line_numbered_snippet(
        content: str,
        focus_terms: list[str],
        *,
        max_snippets: int = 3,
        context_radius: int = 3,
    ) -> list[str]:
        if not content.strip():
            return []
        lines = content.splitlines()
        normalized_terms = [term.strip() for term in focus_terms if term and term.strip()]
        if not normalized_terms:
            return []
        windows: list[tuple[int, int]] = []
        for idx, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line:
                continue
            lowered = raw_line.lower()
            matched = any(term.lower() in lowered for term in normalized_terms)
            if not matched:
                continue
            start = max(0, idx - context_radius)
            end = min(len(lines), idx + context_radius + 1)
            if windows and start <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
            if len(windows) >= max_snippets:
                break
        snippets: list[str] = []
        for start, end in windows:
            block_lines = [f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end)]
            snippets.append("\n".join(block_lines).rstrip())
        return snippets

    @classmethod
    def _focused_context_terms(
        cls,
        path: str,
        *,
        touched_tests: list[str],
        risk_pack: Optional[dict[str, Any]],
    ) -> list[str]:
        risk_pack = risk_pack if isinstance(risk_pack, dict) else {}
        identifiers = [
            str(item).strip()
            for item in (risk_pack.get("changed_identifiers") or [])
            if str(item).strip()
        ]
        if cls._is_test_path(path):
            basename = os.path.basename(path)
            candidates = identifiers + [basename, basename.replace(".py", "")]
            return [item for item in candidates if item]
        primary = [
            str(item).strip()
            for item in (risk_pack.get("touched_non_test_paths") or [])
            if str(item).strip()
        ]
        candidates = identifiers + primary + list(touched_tests[:2])
        return [item for item in candidates if item]

    def build_review_context(
        self,
        workspace_path: str,
        touched_paths: list[str],
        *,
        touched_tests: Optional[list[str]] = None,
        risk_pack: Optional[dict[str, Any]] = None,
        max_files: int = 12,
        max_file_chars: int = 20000,
        max_chars: int = 60000,
    ) -> str:
        """Build a ranked local context pack around the changed hunks, then fall back to full files."""
        if not workspace_path or not isinstance(touched_paths, list):
            return ""
        touched_tests = touched_tests if isinstance(touched_tests, list) else []
        review_targets: list[str] = []
        for item in [*touched_paths, *touched_tests]:
            text = str(item or "").strip()
            if text and text not in review_targets:
                review_targets.append(text)
        sections = [
            f"Local workspace path: {workspace_path}",
            "Hunk-centered local context pack from the PR head commit (authoritative source for your review):",
        ]
        remaining = max_chars
        added = 0
        snippet_paths: set[str] = set()
        for path in review_targets:
            if added >= max_files or remaining <= 400:
                break
            resolved = self._resolve_repo_file(workspace_path, str(path or ""))
            if not resolved or not os.path.isfile(resolved):
                continue
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError:
                continue
            if not content.strip():
                continue
            focus_terms = self._focused_context_terms(
                str(path or ""),
                touched_tests=touched_tests,
                risk_pack=risk_pack,
            )
            snippets = self._line_numbered_snippet(content, focus_terms)
            if snippets:
                block = f"### {path}\n" + "\n\n".join(f"```text\n{snippet}\n```" for snippet in snippets)
                if len(block) <= remaining:
                    sections.append(block)
                    remaining -= len(block) + 2
                    added += 1
                    snippet_paths.add(str(path))
                    continue
        if remaining > 400:
            sections.append("Full contents fallback for changed files at the PR head commit:")
        for path in review_targets:
            if added >= max_files or remaining <= 400:
                break
            if str(path) in snippet_paths:
                continue
            resolved = self._resolve_repo_file(workspace_path, str(path or ""))
            if not resolved or not os.path.isfile(resolved):
                continue
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError:
                continue
            if not content.strip():
                continue
            budget = min(remaining, max_file_chars)
            truncated = len(content) > budget
            body = content[:budget].rstrip() if truncated else content.rstrip()
            block = f"### {path}\n```\n{body}\n```"
            if truncated:
                block += (
                    f"\n[note: file body shown up to {budget} characters for length; "
                    "this is an input-size limit, not a defect in the code]"
                )
            sections.append(block)
            remaining -= len(block) + 2
            added += 1
        if added == 0:
            return ""
        return "\n\n".join(sections)

    @staticmethod
    def _clean_check_env(temp_home: str) -> dict[str, str]:
        """Build a minimal environment for verification subprocesses.

        The reviewer may execute PR-owned code via ``pytest``/``ruff``. Use an
        allowlist plus a throwaway HOME/USERPROFILE so those subprocesses cannot
        read deployment secrets from the bot host environment.
        """
        allowed_passthrough = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "COMSPEC",
            "WINDIR",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PYTHONPATH",
            "PYTHONIOENCODING",
            "PYTHONUTF8",
            "VIRTUAL_ENV",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed_passthrough and value}
        env["HOME"] = temp_home
        env["USERPROFILE"] = temp_home
        env["TMPDIR"] = temp_home
        env["TEMP"] = temp_home
        env["TMP"] = temp_home
        return env

    def _run_check(self, cwd: str, args: list[str], timeout: int, *, env: Optional[dict[str, str]] = None) -> tuple[int, str]:
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=env,
            )
            return result.returncode, (result.stdout + result.stderr).strip()
        except subprocess.TimeoutExpired:
            return -1, f"timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            return -1, str(exc)

    def _resolve_existing_review_targets(self, workspace_path: str, candidates: list[str]) -> list[str]:
        resolved: list[str] = []
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            path = self._resolve_repo_file(workspace_path, text)
            if path and os.path.isfile(path):
                resolved.append(text)
        return resolved

    def verification_policy_note(self, task_data: dict[str, Any]) -> str:
        github_inputs = _review_github_inputs(task_data)
        association = str(github_inputs.get("author_association") or "").strip().upper()
        if not association:
            return ""
        if _review_allow_external_checks():
            return ""
        if association in _review_trusted_associations():
            return ""
        return (
            "Automated verification checks were skipped because this PR was triggered by an "
            f"untrusted contributor association (`{association}`). Treat the diff and workspace "
            "context as read-only evidence unless a trusted maintainer reruns the review."
        )

    def build_verification_report(
        self,
        workspace_path: str,
        touched_paths: list[str],
        touched_tests: list[str],
        *,
        enabled: Optional[bool] = None,
        timeout: Optional[int] = None,
        max_output_chars: int = 2500,
    ) -> str:
        """Run the repo's linters/tests against the checked-out PR head.

        Opt-in via ``MEP_REVIEW_RUN_CHECKS`` because this executes code from the
        PR under review; it should only run inside the isolated per-bridge
        workspace. The captured pass/fail signal is fed to the reviewer so it can
        ground its review in real verification instead of guessing from a diff.
        """
        if enabled is None:
            enabled = _review_run_checks_enabled()
        if not enabled or not workspace_path or not os.path.isdir(workspace_path):
            return ""
        if timeout is None:
            timeout = _env_positive_int("MEP_REVIEW_CHECK_TIMEOUT", 180)
        touched_paths = touched_paths if isinstance(touched_paths, list) else []
        touched_tests = touched_tests if isinstance(touched_tests, list) else []
        py_files = self._resolve_existing_review_targets(
            workspace_path,
            [str(p) for p in touched_paths if str(p).endswith(".py")],
        )

        def _tail(text: str) -> str:
            text = (text or "").strip()
            if len(text) > max_output_chars:
                return "...\n" + text[-max_output_chars:]
            return text

        reports: list[str] = []
        test_targets = self._resolve_existing_review_targets(
            workspace_path,
            [str(t) for t in touched_tests if str(t).strip()],
        )
        if not test_targets:
            test_targets = [p for p in py_files if "test" in os.path.basename(p).lower()]
        with tempfile.TemporaryDirectory(prefix="mep-review-check-") as temp_home:
            check_env = self._clean_check_env(temp_home)
            if py_files and shutil.which("ruff"):
                code, out = self._run_check(
                    workspace_path,
                    ["ruff", "check", *py_files],
                    timeout,
                    env=check_env,
                )
                status = "passed" if code == 0 else f"failed (exit {code})"
                reports.append(f"$ ruff check (changed files): {status}\n{_tail(out)}")

            if test_targets:
                code, out = self._run_check(
                    workspace_path,
                    [sys.executable, "-m", "pytest", *test_targets, "-q"],
                    timeout,
                    env=check_env,
                )
                status = "passed" if code == 0 else f"failed (exit {code})"
                reports.append(f"$ pytest (changed tests): {status}\n{_tail(out)}")

        if not reports:
            return ""
        header = (
            "Automated verification run on the checked-out PR head (authoritative; "
            "use these results in your review):"
        )
        return header + "\n\n" + "\n\n".join(reports)


class RuntimeNode:
    def __init__(self, identity: MEPIdentity, hub_url: str, ws_url: str, adapter: Any, alias: Optional[str] = None):
        self.identity = identity
        self.node_id = identity.node_id
        self.hub_url = hub_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.adapter = adapter
        self.alias = alias
        self.running = True
        self.max_purchase_price = float(os.getenv("MEP_MAX_PURCHASE_PRICE", "0.0"))
        self.live_call_enabled = _env_truthy("MEP_LIVE_CALL_ENABLED")
        self.dm_to_call_bridge_enabled = _env_truthy("MEP_DM_TO_CALL_BRIDGE_ENABLED")
        self.call_auto_accept = _env_truthy("MEP_CALL_AUTO_ACCEPT")
        self.call_invite_timeout_ms = _env_positive_int("MEP_CALL_INVITE_TIMEOUT_MS", 30000)
        self.call_reconnect_grace_ms = _env_positive_int("MEP_CALL_RECONNECT_GRACE_MS", 10000)
        self._ws: Any = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._pending_call_bridges: dict[str, asyncio.Future[dict[str, Any]]] = {}
        
        # Phase 4: Autonomous Workspace Management
        workspace_dir = os.getenv("MEP_WORKSPACE_DIR")
        if not workspace_dir:
            # Fallback to a subfolder in the identity directory
            key_path = getattr(identity, "key_path", "node.pem")
            workspace_dir = os.path.join(os.path.dirname(os.path.abspath(key_path)), "workspace", (alias or "default").replace(" ", "-").lower())
        self.workspace = WorkspaceManager(workspace_dir)

    def _auth_headers(self, payload: str) -> dict[str, str]:
        headers = self.identity.get_auth_headers(payload)
        headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _bridge_metadata(interbot_message: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not isinstance(interbot_message, dict):
            return None
        task = interbot_message.get("task")
        if not isinstance(task, dict):
            return None
        inputs = task.get("inputs")
        if not isinstance(inputs, dict):
            return None
        bridge_metadata = inputs.get("bridge_metadata")
        if not isinstance(bridge_metadata, dict):
            return None
        if str(bridge_metadata.get("source_type") or "").strip().lower() != "github":
            return None
        required = ("bridge_id", "status_endpoint", "status_token")
        if not all(isinstance(bridge_metadata.get(name), str) and str(bridge_metadata.get(name)).strip() for name in required):
            return None
        return bridge_metadata

    @staticmethod
    def _bridge_status_action(
        interbot_message: Optional[dict[str, Any]],
        *,
        detail: Optional[str] = None,
        task_data: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        if not isinstance(interbot_message, dict):
            return None
        intent = interbot_message.get("intent")
        intent_type = intent.get("type") if isinstance(intent, dict) else None
        if intent_type == "code.review.approve":
            if not _approval_detail_supports_publishable_approval(detail, task_data=task_data):
                return "reviewed"
            return "approved"
        if intent_type == "code.review.request":
            if _approval_detail_supports_publishable_approval(detail, task_data=task_data):
                return "approved"
            return "reviewed"
        if intent_type == "code.review.comment":
            return "commented"
        if intent_type in {"analysis.request", "issue.triage.request"}:
            return "commented"
        return None

    def _report_bridge_status(
        self,
        interbot_message: Optional[dict[str, Any]],
        *,
        task_data: Optional[dict[str, Any]],
        task_id: str,
        status: str,
        detail: Optional[str],
    ) -> None:
        bridge_metadata = self._bridge_metadata(interbot_message)
        if bridge_metadata is None:
            return
        conversation = interbot_message.get("conversation") if isinstance(interbot_message, dict) else None
        payload: dict[str, Any] = {
            "bridge_id": bridge_metadata["bridge_id"],
            "status": status,
            "target_node_id": self.node_id,
            "task_id": task_id,
            "timestamp_ms": int(time.time() * 1000),
        }
        if isinstance(conversation, dict) and isinstance(conversation.get("context_id"), str):
            payload["context_id"] = conversation["context_id"]
        action = self._bridge_status_action(interbot_message, detail=detail, task_data=task_data)
        if action and status == "completed":
            payload["action"] = action
        if detail:
            payload["detail"] = detail[:60000]
        code, _body, raw = _safe_request(
            "POST",
            bridge_metadata["status_endpoint"],
            json_body=payload,
            headers={"Authorization": f"Bearer {bridge_metadata['status_token']}"},
            timeout=20.0,
        )
        if code == 200:
            print(f"[mep run] bridge status reported task={task_id[:8]} bridge_id={bridge_metadata['bridge_id']}")
        else:
            print(
                f"[mep run] bridge status failed task={task_id[:8]} "
                f"bridge_id={bridge_metadata['bridge_id']} status={code} detail={raw}"
            )

    def register(self, alias: Optional[str]) -> tuple[bool, str]:
        payload = {
            "pubkey": self.identity.pub_pem,
            "x25519_public_key": self.identity.x25519_public_key,
        }
        if alias:
            payload["alias"] = alias
        code, body, raw = _safe_request("POST", f"{self.hub_url}/register", json_body=payload)
        if code == 200 and body:
            return True, f"registered node_id={body.get('node_id', self.node_id)} balance={body.get('balance')}"
        return False, f"register failed status={code} detail={raw}"

    def bid(self, task_id: str) -> None:
        payload = json.dumps({"task_id": task_id, "provider_id": self.node_id})
        code, _body, raw = _safe_request(
            "POST",
            f"{self.hub_url}/tasks/bid",
            data_body=payload,
            headers=self._auth_headers(payload),
            timeout=15.0,
        )
        if code != 200:
            print(f"[mep run] bid failed task={task_id[:8]} status={code} detail={raw}")

    def should_bid(self, task_data: dict[str, Any]) -> bool:
        try:
            bounty = float(task_data.get("bounty") or 0.0)
        except (TypeError, ValueError):
            return False
        if bounty >= 0:
            return True
        cost = abs(bounty)
        if cost <= self.max_purchase_price:
            return True
        task_id = str(task_data.get("id") or "")
        print(
            f"[mep run] skip data-market task={task_id[:8]} "
            f"cost={cost:.6f} max_purchase_price={self.max_purchase_price:.6f}"
        )
        return False

    def complete(self, task_id: str, result_payload: str) -> None:
        payload = json.dumps(
            {
                "task_id": task_id,
                "provider_id": self.node_id,
                "result_payload": result_payload,
            }
        )
        code, _body, raw = _safe_request(
            "POST",
            f"{self.hub_url}/tasks/complete",
            data_body=payload,
            headers=self._auth_headers(payload),
            timeout=20.0,
        )
        if code == 200:
            print(f"[mep run] completed task={task_id[:8]}")
        else:
            print(f"[mep run] complete failed task={task_id[:8]} status={code} detail={raw}")

    def _schedule_background_task(self, coroutine: Any, *, label: str) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def _cleanup(done_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                done_task.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[mep run] background task error label={label} detail={exc}")

        task.add_done_callback(_cleanup)

    async def _send_ws_event(self, payload: dict[str, Any]) -> bool:
        if self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[mep run] ws send failed event={payload.get('event')} detail={exc}")
            return False

    def _fetch_pending_tasks(self) -> list[dict[str, Any]]:
        code, body, raw = _safe_request(
            "GET",
            f"{self.hub_url}/tasks/pending/{self.node_id}",
            headers=self._auth_headers(""),
            timeout=20.0,
        )
        if code != 200:
            print(f"[mep run] pending task poll failed status={code} detail={raw}")
            return []
        tasks = body.get("tasks") if isinstance(body, dict) else None
        if not isinstance(tasks, list):
            return []
        return [task for task in tasks if isinstance(task, dict)]

    async def _recover_pending_tasks(self) -> None:
        tasks = self._fetch_pending_tasks()
        if not tasks:
            return
        print(f"[mep run] recovered pending tasks count={len(tasks)} node={self.node_id}")
        for task in tasks:
            await self.handle_ws_event({"event": "new_task", "data": task})

    def _resolve_call_bridge(self, context_id: Optional[str], outcome: dict[str, Any]) -> None:
        if not context_id:
            return
        future = self._pending_call_bridges.get(context_id)
        if future is not None and not future.done():
            future.set_result(outcome)

    def _cancel_pending_call_bridges(self, reason: str) -> None:
        for context_id, future in list(self._pending_call_bridges.items()):
            if not future.done():
                future.set_result({"status": "rejected", "reason": reason, "context_id": context_id})

    def _bridge_eligible_interbot_message(
        self, task_data: dict[str, Any], interbot_message: Optional[dict[str, Any]]
    ) -> bool:
        if not (
            self.live_call_enabled
            and self.dm_to_call_bridge_enabled
            and self._ws is not None
            and MEPClient is not None
            and interbot_message is not None
        ):
            return False
        try:
            bounty = float(task_data.get("bounty") or 0.0)
        except (TypeError, ValueError):
            return False
        if bounty != 0.0:
            return False
        source = interbot_message.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("node_id"), str):
            return False
        conversation = interbot_message.get("conversation")
        if not isinstance(conversation, dict) or not isinstance(conversation.get("context_id"), str):
            return False
        return True

    async def _attempt_live_call_bridge(
        self,
        task_data: dict[str, Any],
        interbot_message: dict[str, Any],
        result_payload: str,
    ) -> bool:
        conversation = interbot_message["conversation"]
        source = interbot_message["source"]
        context_id = conversation["context_id"]
        peer_node = source["node_id"]
        if context_id in self._pending_call_bridges:
            print(f"[mep run] live bridge skipped duplicate context={context_id}")
            return False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_call_bridges[context_id] = future
        invite_payload = {
            "event": "call.invite",
            "context_id": context_id,
            "callee": peer_node,
            "timeout_ms": self.call_invite_timeout_ms,
            "reconnect_grace_ms": self.call_reconnect_grace_ms,
            "bridge": {
                "mode": "dm_upgrade",
                "origin_task_id": task_data.get("id"),
                "origin_message_id": interbot_message.get("message_id"),
                "trace_id": interbot_message.get("trace_id"),
                "intent_type": interbot_message.get("intent", {}).get("type"),
            },
        }
        if not await self._send_ws_event(invite_payload):
            self._pending_call_bridges.pop(context_id, None)
            return False

        print(f"[mep run] live bridge invite context={context_id} peer={peer_node}")
        timeout_seconds = max(1.0, self.call_invite_timeout_ms / 1000.0 + 1.0)
        try:
            outcome = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            print(f"[mep run] live bridge timeout context={context_id} peer={peer_node}")
            return False
        finally:
            self._pending_call_bridges.pop(context_id, None)

        if outcome.get("status") != "accepted":
            print(
                f"[mep run] live bridge fallback context={context_id} "
                f"peer={peer_node} reason={outcome.get('reason', 'not_accepted')}"
            )
            return False

        frame_sent = await self._send_ws_event(
            {
                "event": "call.frame",
                "context_id": context_id,
                "seq": 0,
                "content_type": "text/plain",
                "payload": result_payload,
            }
        )
        if not frame_sent:
            print(f"[mep run] live bridge frame send failed context={context_id} peer={peer_node}")
            return False
        hangup_sent = await self._send_ws_event({"event": "call.hangup", "context_id": context_id})
        if not hangup_sent:
            print(f"[mep run] live bridge hangup send failed context={context_id} peer={peer_node}")
        settlement = (
            "LIVE_CALL_BRIDGE_OK\n"
            f"context={context_id}\n"
            f"peer={peer_node}\n"
            "transport=call.frame\n"
            f"origin_task={str(task_data.get('id') or '')[:8]}"
        )
        self.complete(str(task_data.get("id") or ""), settlement)
        print(f"[mep run] live bridge completed context={context_id} peer={peer_node}")
        return True

    async def _handle_call_event(self, data: dict[str, Any]) -> None:
        event = str(data.get("event") or "")
        context_id = data.get("context_id") if isinstance(data.get("context_id"), str) else None
        if event == "call.ping":
            if context_id:
                await self._send_ws_event({"event": "call.pong", "context_id": context_id})
            return
        if event == "call.incoming":
            caller = data.get("caller") if isinstance(data.get("caller"), str) else None
            if not context_id or not caller:
                return
            if self.live_call_enabled and self.call_auto_accept:
                sent = await self._send_ws_event({"event": "call.accept", "context_id": context_id})
                if sent:
                    print(f"[mep run] auto-accepted live call context={context_id} caller={caller}")
                else:
                    print(f"[mep run] auto-accept failed context={context_id} caller={caller}")
            else:
                sent = await self._send_ws_event(
                    {"event": "call.decline", "context_id": context_id, "reason": "manual_required"}
                )
                if sent:
                    print(f"[mep run] declined live call context={context_id} caller={caller}")
                else:
                    print(f"[mep run] live call decline failed context={context_id} caller={caller}")
            return
        if event == "call.accepted":
            self._resolve_call_bridge(context_id, {"status": "accepted", "context_id": context_id})
            return
        if event in {"call.declined", "call.timeout", "call.rejected", "call.cancelled"}:
            self._resolve_call_bridge(
                context_id,
                {
                    "status": "rejected",
                    "reason": data.get("reason") or event.removeprefix("call."),
                    "context_id": context_id,
                },
            )
            return
        if event == "call.frame":
            sender = data.get("sender") if isinstance(data.get("sender"), str) else "unknown"
            snippet = str(data.get("payload") or "").strip().replace("\n", " ")[:160]
            print(f"[mep run] live frame context={context_id} sender={sender} payload={snippet}")
            return
        if event in {"call.hangup", "call.suspended", "call.resumed"}:
            print(f"[mep run] {event} context={context_id} detail={data}")

    @staticmethod
    def _interbot_reply_mode(interbot_message: dict[str, Any]) -> Optional[str]:
        delivery = interbot_message.get("delivery")
        if isinstance(delivery, dict) and isinstance(delivery.get("reply_mode"), str):
            return delivery["reply_mode"]
        return None

    @staticmethod
    def _interbot_source_node(interbot_message: dict[str, Any]) -> Optional[str]:
        source = interbot_message.get("source")
        if isinstance(source, dict) and isinstance(source.get("node_id"), str):
            return source["node_id"]
        return None

    def _can_use_structured_dm_fallback(self, interbot_message: Optional[dict[str, Any]]) -> bool:
        if MEPClient is None or interbot_message is None:
            return False
        if self._interbot_reply_mode(interbot_message) != "new_dm":
            return False
        # Bound auto-replies to declared safe threads to avoid unbounded bot loops.
        return MEPClient.extract_session_safety(json.dumps(interbot_message)) is not None

    def _build_interbot_message(
        self,
        message: str,
        target_node: str,
        *,
        context_id: str,
        reply_to_task_id: Optional[str],
        reply_to_message_id: Optional[str],
        turn_type: str,
        intent_type: str,
        priority: str,
        trace_id: Optional[str],
        session_safety: Optional[dict[str, Any]],
        turn_index: Optional[int],
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        task: dict[str, Any] = {
            "instructions": message,
            "expected_output": {"result_type": "text"},
        }
        inputs: dict[str, Any] = {}
        if session_safety:
            normalized_session_safety = dict(session_safety)
            if "started_at_ms" not in normalized_session_safety:
                normalized_session_safety["started_at_ms"] = timestamp_ms
            inputs["session_safety"] = normalized_session_safety
        if inputs:
            task["inputs"] = inputs
        conversation: dict[str, Any] = {
            "context_id": context_id,
            "reply_to_task_id": reply_to_task_id,
            "reply_to_message_id": reply_to_message_id,
            "turn_type": turn_type,
        }
        if turn_index is not None:
            conversation["turn_index"] = turn_index
        return {
            "spec_version": "mep.interbot.v1",
            "message_id": message_id,
            "trace_id": trace_id or str(uuid.uuid4()),
            "timestamp_ms": timestamp_ms,
            "source": {"node_id": self.node_id},
            "target": {"node_id": target_node},
            "conversation": conversation,
            "intent": {"type": intent_type, "priority": priority},
            "task": task,
            "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
            "delivery": {"reply_mode": "new_dm", "settlement_mode": "task_result"},
        }

    def _build_interbot_reply_message(
        self,
        reply_text: str,
        inbound_message: dict[str, Any],
        *,
        inbound_task_id: Optional[str],
        turn_type: Optional[str] = None,
        intent_type: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> dict[str, Any]:
        if MEPClient is None:
            raise RuntimeError("MEPClient is unavailable")
        source = inbound_message.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("node_id"), str):
            raise ValueError("inbound inter-bot message is missing source.node_id")
        inbound_intent = inbound_message.get("intent")
        conversation = inbound_message.get("conversation")
        inbound_priority = (
            inbound_intent.get("priority")
            if isinstance(inbound_intent, dict) and isinstance(inbound_intent.get("priority"), str)
            else "normal"
        )
        inbound_turn_type = conversation.get("turn_type") if isinstance(conversation, dict) else None
        return self._build_interbot_message(
            reply_text,
            source["node_id"],
            context_id=conversation.get("context_id") if isinstance(conversation, dict) else str(uuid.uuid4()),
            reply_to_task_id=inbound_task_id,
            reply_to_message_id=inbound_message.get("message_id")
            if isinstance(inbound_message.get("message_id"), str)
            else None,
            turn_type=turn_type or MEPClient._default_reply_turn_type(inbound_turn_type),
            intent_type=intent_type
            or MEPClient._default_reply_intent_type(
                inbound_intent.get("type") if isinstance(inbound_intent, dict) else None
            ),
            priority=priority or inbound_priority,
            trace_id=inbound_message.get("trace_id") if isinstance(inbound_message.get("trace_id"), str) else None,
            session_safety=MEPClient._extract_session_safety_from_message(inbound_message),
            turn_index=MEPClient._derive_reply_turn_index(inbound_message),
        )

    def _build_checkpoint_message(
        self,
        summary: str,
        target_node: str,
        *,
        context_id: str,
        reply_to_task_id: Optional[str],
        reply_to_message_id: Optional[str],
        priority: str,
        session_safety: Optional[dict[str, Any]],
        turn_index: Optional[int],
        trace_id: Optional[str],
    ) -> dict[str, Any]:
        return self._build_interbot_message(
            summary,
            target_node,
            context_id=context_id,
            reply_to_task_id=reply_to_task_id,
            reply_to_message_id=reply_to_message_id,
            turn_type="checkpoint",
            intent_type="coordination.request",
            priority=priority,
            trace_id=trace_id,
            session_safety=session_safety,
            turn_index=turn_index,
        )

    def _submit_structured_interbot_message(self, envelope: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
        target = envelope.get("target", {})
        target_node = target.get("node_id") if isinstance(target, dict) else None
        if not isinstance(target_node, str) or not target_node:
            return False, {}, "missing target.node_id"
        outer = build_task_envelope(self.node_id, json.dumps(envelope), 0.0, target_node=target_node)
        payload = json.dumps(outer)
        code, body, raw = _safe_request(
            "POST",
            f"{self.hub_url}/tasks/submit",
            data_body=payload,
            headers=self._auth_headers(payload),
            timeout=20.0,
        )
        if code == 200 and body:
            return True, body, raw
        return False, body or {}, raw

    async def _submit_safe_structured_dm_reply(
        self,
        reply_text: str,
        inbound_message: dict[str, Any],
        *,
        inbound_task_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if MEPClient is None or not self._can_use_structured_dm_fallback(inbound_message):
            return None
        next_turn_index = MEPClient._derive_reply_turn_index(inbound_message)
        if next_turn_index is None:
            return None
        evaluation = MEPClient.evaluate_interbot_session_safety_message(
            inbound_message,
            next_turn_index=next_turn_index,
        )
        context_id = MEPClient._extract_context_id(inbound_message)
        session_safety = MEPClient._extract_session_safety_from_message(inbound_message)
        if evaluation["should_stop"]:
            return {
                "status": "stopped",
                "reply_action": "stop",
                "context_id": context_id,
                "session_safety": session_safety,
                "safety": evaluation,
            }
        source = inbound_message.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("node_id"), str):
            return None
        inbound_priority = inbound_message.get("intent", {}).get("priority") if isinstance(inbound_message.get("intent"), dict) else "normal"
        if evaluation["should_checkpoint"]:
            envelope = self._build_checkpoint_message(
                f"Checkpoint: session reached turn {next_turn_index}. Confirm whether to continue.",
                source["node_id"],
                context_id=context_id or str(uuid.uuid4()),
                reply_to_task_id=inbound_task_id,
                reply_to_message_id=inbound_message.get("message_id")
                if isinstance(inbound_message.get("message_id"), str)
                else None,
                priority=inbound_priority if isinstance(inbound_priority, str) else "normal",
                session_safety=session_safety,
                turn_index=next_turn_index,
                trace_id=inbound_message.get("trace_id") if isinstance(inbound_message.get("trace_id"), str) else None,
            )
            ok, body, raw = self._submit_structured_interbot_message(envelope)
            if not ok:
                print(f"[mep run] structured checkpoint failed context={context_id} detail={raw}")
                return None
            return {
                "status": "checkpointed",
                "reply_action": "checkpoint",
                "context_id": context_id,
                "message_id": envelope["message_id"],
                "trace_id": envelope["trace_id"],
                "task_id": body.get("task_id"),
                "session_safety": session_safety,
                "safety": evaluation,
            }
        envelope = self._build_interbot_reply_message(reply_text, inbound_message, inbound_task_id=inbound_task_id)
        ok, body, raw = self._submit_structured_interbot_message(envelope)
        if not ok:
            print(f"[mep run] structured dm reply failed context={context_id} detail={raw}")
            return None
        return {
            "status": "replied",
            "reply_action": "reply",
            "context_id": context_id,
            "message_id": envelope["message_id"],
            "trace_id": envelope["trace_id"],
            "task_id": body.get("task_id"),
            "session_safety": session_safety,
            "safety": evaluation,
        }

    @staticmethod
    def _dm_fallback_settlement(task_id: str, dm_response: dict[str, Any]) -> str:
        action = dm_response.get("reply_action", "unknown")
        context_id = dm_response.get("context_id") or "unknown"
        if action == "reply":
            return (
                "DM_REPLY_SENT\n"
                f"context={context_id}\n"
                f"reply_task={dm_response.get('task_id') or '?'}\n"
                f"origin_task={task_id[:8]}"
            )
        if action == "checkpoint":
            return (
                "DM_CHECKPOINT_SENT\n"
                f"context={context_id}\n"
                f"reply_task={dm_response.get('task_id') or '?'}\n"
                f"origin_task={task_id[:8]}"
            )
        violations = ",".join(dm_response.get("safety", {}).get("violations", [])) or "session_limits"
        return (
            "DM_REPLY_STOPPED\n"
            f"context={context_id}\n"
            f"reason={violations}\n"
            f"origin_task={task_id[:8]}"
        )

    async def process_task(self, task_data: dict[str, Any]) -> None:
        task_id = str(task_data.get("id") or "")
        payload = str(task_data.get("payload") or "")
        instructions = payload
        adapter_task_data = copy.deepcopy(task_data)
        interbot_message: Optional[dict[str, Any]] = None
        if MEPClient is not None:
            instructions, interbot_message = MEPClient.extract_interbot_instructions(payload)
        workspace_path = ""
        task = task_data.get("task") if isinstance(task_data.get("task"), dict) else {}
        if not task and isinstance(interbot_message, dict) and isinstance(interbot_message.get("task"), dict):
            task = copy.deepcopy(interbot_message.get("task"))
        inputs = task.get("inputs") if isinstance(task.get("inputs"), dict) else {}
        github_inputs = dict(inputs.get("github") or {}) if isinstance(inputs.get("github"), dict) else {}
        repo_audit_inputs = dict(inputs.get("repo_audit") or {}) if isinstance(inputs.get("repo_audit"), dict) else {}
        repo_audit_required = _task_requires_repo_audit_contract(task_data)
        if repo_audit_required:
            print(
                "[mep repo_audit] task contract "
                f"task={task_id[:8]} "
                f"intent={_review_intent_type(task_data) or '-'} "
                f"model_requirement={_task_model_requirement(task_data) or '-'} "
                f"title={_task_title(task_data)[:120] or '-'} "
                f"has_repo_inputs={bool(repo_audit_inputs)}"
            )
            failure = _repo_audit_contract_failure(task_data)
            if failure:
                print(f"[mep repo_audit] refusing task={task_id[:8]} reason={failure}")
                self.complete(task_id, failure)
                return

        # Phase 4: Sync workspace if this is a GitHub PR task
        if interbot_message:
            repo_url = github_inputs.get("repo_clone_url")
            head_sha = github_inputs.get("head_sha")
            head_ref = github_inputs.get("head_ref")
            bridge_id = interbot_message.get("trace_id") or interbot_message.get("task", {}).get("inputs", {}).get("bridge_metadata", {}).get("bridge_id")

            if repo_url and head_sha and head_ref:
                print(f"[mep workspace] auto-syncing for task={task_id[:8]} bridge_id={bridge_id}")
                ok, path_or_err = await asyncio.to_thread(self.workspace.sync_pr_workspace, repo_url, head_sha, head_ref, bridge_id=bridge_id)
                if ok:
                    print(f"[mep workspace] synced to {path_or_err}")
                    workspace_path = path_or_err
                    github_inputs["local_workspace_path"] = workspace_path
                    touched_paths = github_inputs.get("touched_paths") if isinstance(github_inputs.get("touched_paths"), list) else []
                    touched_tests = github_inputs.get("touched_tests") if isinstance(github_inputs.get("touched_tests"), list) else []
                    workspace_context = await asyncio.to_thread(
                        self.workspace.build_review_context,
                        workspace_path,
                        touched_paths,
                        touched_tests=touched_tests,
                        risk_pack=github_inputs.get("risk_pack"),
                    )
                    if workspace_context:
                        instructions = f"{instructions}\n\nAdditional local workspace context:\n{workspace_context}"
                    verification_note = self.workspace.verification_policy_note(adapter_task_data)
                    if verification_note:
                        instructions = f"{instructions}\n\n{verification_note}"
                    else:
                        verification_report = await asyncio.to_thread(
                            self.workspace.build_verification_report,
                            workspace_path,
                            touched_paths,
                            touched_tests,
                        )
                        if verification_report:
                            instructions = f"{instructions}\n\n{verification_report}"
                else:
                    print(f"[mep workspace] sync failed: {path_or_err}")
                    # We continue anyway, but the adapter might be working on stale code

            intent = interbot_message.get("intent")
            if isinstance(intent, dict):
                adapter_task_data["intent"] = copy.deepcopy(intent)

        repo_url = str(repo_audit_inputs.get("repo_url") or "").strip()
        repo_ref = str(repo_audit_inputs.get("ref") or "").strip()
        if repo_url:
            print(f"[mep repo_audit] auto-syncing for task={task_id[:8]} repo={repo_url}")
            ok, path_or_err = await asyncio.to_thread(self.workspace.sync_repo_audit_workspace, repo_url, repo_ref or None)
            if ok:
                repo_workspace_path = path_or_err
                repo_audit_inputs["local_workspace_path"] = repo_workspace_path
                audit_context, inventory_paths = await asyncio.to_thread(
                    self.workspace.build_repo_audit_context,
                    repo_workspace_path,
                )
                if not inventory_paths:
                    detail = "[repo audit] workspace context missing: tracked-file inventory unavailable"
                    print(f"[mep repo_audit] refusing task={task_id[:8]} reason={detail}")
                    self.complete(task_id, detail)
                    return
                repo_audit_inputs["inventory_paths"] = inventory_paths
                if not audit_context:
                    detail = "[repo audit] workspace context missing: authoritative repo audit context unavailable"
                    print(f"[mep repo_audit] refusing task={task_id[:8]} reason={detail}")
                    self.complete(task_id, detail)
                    return
                instructions = f"{instructions}\n\nAuthoritative local repo audit context:\n{audit_context}"
            else:
                print(f"[mep repo_audit] sync failed: {path_or_err}")
                self.complete(task_id, f"[repo audit] workspace sync failed: {path_or_err}")
                return

        task_copy = copy.deepcopy(task) if isinstance(task, dict) else {}
        inputs_copy = copy.deepcopy(inputs) if isinstance(inputs, dict) else {}
        if github_inputs:
            inputs_copy["github"] = github_inputs
        if repo_audit_inputs:
            inputs_copy["repo_audit"] = repo_audit_inputs
        if inputs_copy:
            task_copy["inputs"] = inputs_copy
        if task_copy:
            adapter_task_data["task"] = task_copy
        if repo_audit_required and not _review_intent_type(adapter_task_data):
            adapter_task_data["intent"] = {"type": "repo_audit.request"}
        if repo_audit_required:
            failure = _repo_audit_contract_failure(adapter_task_data)
            if failure:
                print(f"[mep repo_audit] refusing normalized task={task_id[:8]} reason={failure}")
                self.complete(task_id, failure)
                return
            normalized_repo_audit = _repo_audit_inputs(adapter_task_data)
            if not str(normalized_repo_audit.get("local_workspace_path") or "").strip():
                detail = "[repo audit] local workspace path missing after task normalization"
                print(f"[mep repo_audit] refusing normalized task={task_id[:8]} reason={detail}")
                self.complete(task_id, detail)
                return
            if not _clean_review_list(normalized_repo_audit.get("inventory_paths"), max_items=300, max_chars=160):
                detail = "[repo audit] tracked-file inventory missing after task normalization"
                print(f"[mep repo_audit] refusing normalized task={task_id[:8]} reason={detail}")
                self.complete(task_id, detail)
                return

        result = self.adapter.generate_reply(instructions, adapter_task_data)

        # Fail-safe: a reviewer runtime must never let an adapter error (missing or
        # expired API key, HTTP error, timeout, empty completion) be written back as
        # a completed review/approval. Report a failed status with no review action so
        # the bridge does not publish a decision built on error text.
        if (
            interbot_message is not None
            and self._bridge_status_action(interbot_message, detail=result, task_data=task_data) is not None
            and _is_adapter_error(result)
        ):
            self.complete(task_id, result)
            self._report_bridge_status(
                interbot_message,
                task_data=task_data,
                task_id=task_id,
                status="failed",
                detail=f"Reviewer runtime produced no publishable review; adapter error: {result[:1000]}",
            )
            return

        if self._bridge_eligible_interbot_message(task_data, interbot_message):
            bridged = await self._attempt_live_call_bridge(task_data, interbot_message, result)
            if bridged:
                self._report_bridge_status(
                    interbot_message,
                    task_data=task_data,
                    task_id=task_id,
                    status="completed",
                    detail=result,
                )
                return
        dm_response = await self._submit_safe_structured_dm_reply(
            result,
            interbot_message,
            inbound_task_id=task_id,
        )
        if dm_response is not None:
            self.complete(task_id, self._dm_fallback_settlement(task_id, dm_response))
            self._report_bridge_status(
                interbot_message,
                task_data=task_data,
                task_id=task_id,
                status="completed",
                detail=result,
            )
            return
        self.complete(task_id, result)
        self._report_bridge_status(
            interbot_message,
            task_data=task_data,
            task_id=task_id,
            status="completed",
            detail=result,
        )

    def _ws_uri(self) -> str:
        ts = str(int(time.time()))
        sig = urllib.parse.quote(self.identity.sign(self.node_id, ts))
        return f"{self.ws_url}/ws/{self.node_id}?timestamp={ts}&signature={sig}"

    async def handle_ws_event(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        if event == "rfc":
            task = data.get("data", {})
            task_id = str(task.get("id") or "")
            if task_id and self.should_bid(task):
                self.bid(task_id)
        elif event == "new_task":
            self._schedule_background_task(self.process_task(data.get("data", {})), label="process_task")
        elif isinstance(event, str) and event.startswith("call."):
            await self._handle_call_event(data)

    async def _recv_loop(self, ws: Any) -> None:
        while self.running:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.ping()
                continue
            await self.handle_ws_event(json.loads(msg))

    async def run_forever(self) -> int:
        try:
            try:
                from node.ws_connect import ws_connect
            except ImportError:  # pragma: no cover - supports direct file execution
                from ws_connect import ws_connect
        except ImportError:
            print("[mep run] missing optional dependency: websockets")
            print("[mep run] install with: pip install websockets")
            return 2

        ok, message = self.register(alias=self.alias)
        print(f"[mep run] {message}")
        if not ok:
            return 2
        while self.running:
            uri = self._ws_uri()
            try:
                async with ws_connect(uri) as ws:
                    self._ws = ws
                    print(f"[mep run] connected ws node={self.node_id}")
                    await self._recover_pending_tasks()
                    await self._recv_loop(ws)
            except KeyboardInterrupt:
                self.running = False
            except Exception as exc:  # noqa: BLE001
                print(f"[mep run] websocket reconnect after error: {exc}")
                await asyncio.sleep(3.0)
            finally:
                self._ws = None
                self._cancel_pending_call_bridges("socket_closed")
                for task in list(self._background_tasks):
                    task.cancel()
                if self._background_tasks:
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
        return 0


def _print_badges(badges: dict[str, bool]) -> None:
    parts = [f"{name}={'OK' if status else 'FAIL'}" for name, status in badges.items()]
    print("[mep status] " + " | ".join(parts))


def _print_listener_hint(args: argparse.Namespace) -> None:
    cmd = (
        "python -m node.mep_runtime "
        f"--hub-url {args.hub_url} "
        f"--ws-url {args.ws_url} "
        f"--key-path {args.key_path} run"
    )
    print("[mep status] node is registered, but listener is not running.")
    print("[mep status] start live listener with:")
    print(f"  $ {cmd}")


def cmd_init(args: argparse.Namespace) -> int:
    _ensure_key_parent(args.key_path)
    identity = MEPIdentity(args.key_path)
    alias = _resolve_runtime_alias(args.key_path, args.alias, node_id=identity.node_id)
    print(f"[mep init] node_id={identity.node_id}")
    if identity.generated_new_key:
        print(f"[mep init] generated key={identity.key_path}")
    payload = {
        "pubkey": identity.pub_pem,
        "alias": alias,
        "x25519_public_key": identity.x25519_public_key,
    }
    code, body, raw = _safe_request("POST", f"{args.hub_url.rstrip('/')}/register", json_body=payload)
    if code != 200:
        print(f"[mep init] register failed status={code} detail={raw}")
        return 2
    _write_alias_sidecar(args.key_path, alias)
    print(f"[mep init] register ok balance={body.get('balance') if body else '?'}")
    status_args = argparse.Namespace(
        hub_url=args.hub_url,
        ws_url=args.ws_url,
        key_path=args.key_path,
        adapter=args.adapter,
        require_online=False,
    )
    return cmd_status(status_args)


def cmd_status(args: argparse.Namespace) -> int:
    identity = MEPIdentity(args.key_path)
    node_id = identity.node_id
    url = f"{args.hub_url.rstrip('/')}/diagnostic?node_id={node_id}"
    code, body, raw = _safe_request("GET", url)
    if code != 200 or not body:
        print(f"[mep status] diagnostic failed status={code} detail={raw}")
        return 2
    badges = _status_badges(body, ai_ready=args.adapter != "mock")
    _print_badges(badges)
    if badges["REGISTERED"] and not badges["WS_CONNECTED"]:
        _print_listener_hint(args)
    if args.require_online:
        return 0 if all(badges.values()) else 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    identity = MEPIdentity(args.key_path)
    node_id = identity.node_id
    diag_url = f"{args.hub_url.rstrip('/')}/diagnostic?node_id={node_id}"
    code, diag, raw = _safe_request("GET", diag_url)
    if code != 200 or not diag:
        print(f"[mep doctor] diagnostic failed status={code} detail={raw}")
        return 2

    snapshot = _build_doctor_snapshot(
        node_id=node_id,
        diag=diag,
        auth_status=args.auth_status,
        dm_status=args.dm_status,
        listener_contract_ok=args.listener_contract_ok,
        ai_configured=args.adapter != "mock",
        clock_skew_seconds=args.clock_skew_seconds,
    )
    code, result, raw = _safe_request(
        "POST",
        f"{args.hub_url.rstrip('/')}/onboard/diagnose",
        json_body=snapshot,
    )
    if code != 200 or not result:
        print(f"[mep doctor] diagnose failed status={code} detail={raw}")
        return 2

    print(f"[mep doctor] root_cause={result.get('root_cause')} severity={result.get('severity')}")
    for step in result.get("fix_steps", []):
        print(f"  - {step}")
    for cmd in result.get("copy_paste_commands", []):
        print(f"  $ {cmd}")
    telemetry = result.get("telemetry") or {}
    if telemetry:
        print(
            f"[mep doctor] telemetry total={telemetry.get('total_requests')} "
            f"root_cause_count={telemetry.get('root_cause_count')}"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _ensure_key_parent(args.key_path)
    if args.adapter == "deepseek":
        api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not api_key:
            if _strict_adapter_mode():
                print("[mep run] DEEPSEEK_API_KEY not set; strict adapter mode refusing mock fallback")
                return 2
            print("[mep run] DEEPSEEK_API_KEY not set, falling back to mock")
            adapter: Any = MockAdapter()
        else:
            adapter = DeepSeekAdapter(
                api_key=api_key,
                model=os.getenv("MEP_AI_MODEL", "deepseek-chat"),
            )
            print(f"[mep run] adapter=deepseek model={adapter.model}")
    elif args.adapter == "openai":
        api_key = (os.getenv("OPENAI_COMPAT_API_KEY") or os.getenv("MIMO_API_KEY") or "").strip()
        base_url = (os.getenv("OPENAI_COMPAT_BASE_URL") or "").strip()
        if not api_key or not base_url:
            if _strict_adapter_mode():
                print("[mep run] OPENAI_COMPAT_API_KEY/MIMO_API_KEY or OPENAI_COMPAT_BASE_URL not set; strict adapter mode refusing mock fallback")
                return 2
            print("[mep run] OpenAI-compatible adapter config not set, falling back to mock")
            adapter = MockAdapter()
        else:
            provider_name = (os.getenv("OPENAI_COMPAT_PROVIDER_NAME") or "openai-compatible").strip() or "openai-compatible"
            adapter = OpenAICompatibleAdapter(
                api_key=api_key,
                base_url=base_url,
                model=os.getenv("MEP_AI_MODEL", "gpt-4o-mini"),
                provider_name=provider_name,
            )
            print(f"[mep run] adapter=openai provider={provider_name} model={adapter.model} base_url={base_url}")
    elif args.adapter == "ollama":
        adapter = AIAdapter(model=os.getenv("MEP_AI_MODEL", "tinyllama"))
        print(f"[mep run] adapter=ollama model={adapter.model}")
    elif args.adapter != "mock":
        print("[mep run] unsupported adapter, using mock")
        adapter = MockAdapter()
    else:
        adapter = MockAdapter()
    identity = MEPIdentity(args.key_path)
    alias = _resolve_runtime_alias(args.key_path, args.alias, node_id=identity.node_id)
    runtime = RuntimeNode(
        identity=identity,
        hub_url=args.hub_url,
        ws_url=args.ws_url,
        adapter=adapter,
        alias=alias,
    )
    print(f"[mep run] adapter={args.adapter} node_id={identity.node_id} alias={alias}")
    try:
        return asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        print("[mep run] stopped by user")
        return 0


def cmd_up(args: argparse.Namespace) -> int:
    print("[mep up] bootstrapping node with init -> doctor -> run")
    init_args = argparse.Namespace(
        hub_url=args.hub_url,
        ws_url=args.ws_url,
        key_path=args.key_path,
        adapter=args.adapter,
        alias=args.alias,
    )
    init_code = cmd_init(init_args)
    if init_code != 0:
        return init_code

    doctor_args = argparse.Namespace(
        hub_url=args.hub_url,
        key_path=args.key_path,
        adapter=args.adapter,
        auth_status="ok",
        dm_status="ok",
        listener_contract_ok=None,
        clock_skew_seconds=None,
    )
    doctor_code = cmd_doctor(doctor_args)
    if doctor_code != 0:
        print("[mep up] doctor failed; continuing to run listener for live connectivity")

    return cmd_run(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MEP unified runtime for fast onboarding.")
    parser.add_argument("--hub-url", default=DEFAULT_HUB_URL, help="Hub base URL.")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help="Hub websocket URL.")
    parser.add_argument(
        "--key-path",
        default=None,
        help="Path to provider private key (defaults to repo-local .mep/{node_id}.pem after discovery/provisioning).",
    )
    parser.add_argument("--adapter", default="mock", choices=["mock", "ollama", "deepseek", "openai"], help="Provider adapter.")

    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Generate/load key and register node.")
    init_p.add_argument("--alias", default=None, help="Node alias for registration; defaults to the node_id if no persisted alias exists.")
    init_p.set_defaults(func=cmd_init)

    up_p = sub.add_parser("up", help="One-command bootstrap: init + doctor + run.")
    up_p.add_argument("--alias", default=None, help="Node alias for registration; defaults to the node_id if no persisted alias exists.")
    up_p.set_defaults(func=cmd_up)

    run_p = sub.add_parser("run", help="Run standardized listener runtime.")
    run_p.add_argument("--alias", default=None, help="Node alias for registration; defaults to persisted alias or node_id if none exists.")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="Show quick node readiness badges.")
    status_p.add_argument("--require-online", action="store_true", help="Return non-zero unless all badges pass.")
    status_p.set_defaults(func=cmd_status)

    doctor_p = sub.add_parser("doctor", help="Run onboarding diagnostics against Hub.")
    doctor_p.add_argument("--auth-status", default="ok", help="Override auth status signal.")
    doctor_p.add_argument("--dm-status", default="ok", help="Override DM status signal.")
    doctor_p.add_argument(
        "--listener-contract-ok",
        dest="listener_contract_ok",
        action="store_true",
        default=None,
        help="Set listener contract signal to true.",
    )
    doctor_p.add_argument(
        "--listener-contract-bad",
        dest="listener_contract_ok",
        action="store_false",
        help="Set listener contract signal to false.",
    )
    doctor_p.add_argument("--clock-skew-seconds", type=float, default=None, help="Override local clock skew.")
    doctor_p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "key_path", None) is None:
        try:
            args.key_path = _resolve_default_runtime_key_path(
                command=str(getattr(args, "command", "") or ""),
                cli_alias=getattr(args, "alias", None),
            )
        except RuntimeKeyPathError as exc:
            print(f"[mep {args.command}] {exc}")
            return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
