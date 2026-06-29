#!/usr/bin/env python3
"""Unified node runtime for fast onboarding (`init`, `up`, `run`, `status`, `doctor`)."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import subprocess
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


DEFAULT_HUB_URL = os.getenv("HUB_URL", "http://localhost:8000")
DEFAULT_WS_URL = os.getenv("WS_URL", "ws://localhost:8000")
LEGACY_RUNTIME_KEY_NAME = "mep_runtime.pem"


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) not in ("0", "false", "False", "")


def _strict_adapter_mode() -> bool:
    return _env_truthy("MEP_STRICT_ADAPTERS", "0")


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
    explicit = os.getenv("MEP_KEY_DIR")
    if explicit:
        return explicit
    git_root = _find_git_root()
    if git_root:
        return os.path.join(git_root, ".mep")
    return os.path.join(os.path.expanduser("~"), ".mep")


def _default_key_path() -> str:
    explicit = os.getenv("MEP_PROVIDER_KEY_PATH")
    if explicit:
        return explicit
    return os.path.join(_default_key_dir(), LEGACY_RUNTIME_KEY_NAME)


def _ensure_key_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _canonical_key_path(key_dir: str, node_id: str) -> str:
    return os.path.join(key_dir, f"{node_id}.pem")


def _enc_key_path(key_path: str) -> str:
    return key_path.replace(".pem", "_enc.pem")


def _pending_key_path(key_dir: str) -> str:
    return os.path.join(key_dir, f".pending-runtime-{os.getpid()}-{int(time.time() * 1000)}.pem")


def _is_identity_key_file(filename: str) -> bool:
    return (
        filename.endswith(".pem")
        and not filename.endswith("_enc.pem")
        and not filename.startswith(".pending-runtime-")
    )


def _list_local_identity_key_paths(key_dir: str) -> list[str]:
    if not os.path.isdir(key_dir):
        return []
    return [
        os.path.join(key_dir, name)
        for name in sorted(os.listdir(key_dir))
        if _is_identity_key_file(name) and os.path.isfile(os.path.join(key_dir, name))
    ]


def _move_file_if_present(source: str, destination: str) -> None:
    if _same_path(source, destination) or not os.path.exists(source):
        return
    _ensure_key_parent(destination)
    os.replace(source, destination)


def _alias_sidecar_path(key_path: str) -> str:
    return f"{key_path}.alias"


def _write_alias_sidecar(key_path: str, alias: str) -> None:
    with open(_alias_sidecar_path(key_path), "w", encoding="utf-8") as handle:
        handle.write(alias.strip() + "\n")


def _read_alias_sidecar(key_path: str) -> Optional[str]:
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


class RuntimeKeyPathError(ValueError):
    """Raised when runtime identity selection is ambiguous or missing."""


def _canonicalize_local_identity(key_path: str, key_dir: str) -> str:
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
    os.makedirs(key_dir, exist_ok=True)
    pending_path = _pending_key_path(key_dir)
    return _canonicalize_local_identity(pending_path, key_dir)


def _resolve_default_runtime_key_path(command: str, cli_alias: Optional[str]) -> str:
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


def _system_prompt_for_task(
    task_data: dict[str, Any],
    *,
    generic_max_chars: int,
    review_max_chars: int,
) -> str:
    if _task_requires_review_prompt(task_data):
        github_inputs = _review_github_inputs(task_data)
        approval_mode = _task_is_approval_review(task_data)
        workspace_path = str(github_inputs.get("local_workspace_path") or "").strip()
        workspace_hint = ""
        if workspace_path:
            workspace_hint = (
                f" A checked-out local workspace is available at `{workspace_path}` and any embedded workspace excerpts "
                "come from the PR head commit; treat that material as authoritative code context."
            )
        approval_hint = ""
        if approval_mode:
            approval_hint = (
                " Approval mode is active. Only use `approval_recommendation: \"approve\"` when you can cite at least two exact identifiers from changed lines "
                "in `verified_identifiers`, mention the changed tests when any are provided, and explicitly state the scope is low-risk. "
                "If the supplied PR checks are pending or failing, use `comment` instead of `approve`. "
                "If you cannot satisfy that evidence bar, use `comment` instead of `approve`."
            )
        return (
            "You are a senior code reviewer for the MEP (Miao Exchange Protocol) project. "
            "Review the provided GitHub PR context and return ONLY a JSON object with this schema: "
            '{"summary": string, "observation": string, "touched_paths": [string], "tests_reviewed": [string], '
            '"verified_identifiers": [string], '
            '"findings": [{"file": string, "issue": string, "rationale": string}], '
            '"approval_recommendation": "approve" | "comment" | "request_changes" | "abstain"}. '
            "Use at most 2 findings. "
            "Use `observation` for one concrete non-blocking review note tied to the actual diff. "
            "Use `verified_identifiers` for exact function/variable/class names copied from changed lines in the supplied diff or workspace excerpts. "
            "Prefer real touched files and tests from the supplied GitHub inputs for `touched_paths` and `tests_reviewed`. "
            "Only include a finding when it is directly supported by the provided diff, file list, PR description, or patch excerpts. "
            "Do not speculate about unseen code, do not ask for more context, and do not include chain-of-thought or any text outside the JSON object. "
            "If the change looks good, keep findings empty and use summary to say what you verified, keep observation concrete, and set approval_recommendation to approve or comment. Keep the "
            f"response within {review_max_chars} characters.{approval_hint}{workspace_hint}"
        )
    return (
        "You are a helpful MEP (Miao Exchange Protocol) bot. "
        "MEP is an AI-to-AI economy protocol where agents earn SECONDS by doing work. "
        f"Reply concisely (max {generic_max_chars} chars)."
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


def _is_weak_review_text(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    return any(re.search(pattern, lowered) for pattern in _WEAK_REVIEW_PATTERNS)


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


def _render_structured_review_with_task_data(
    text: str,
    *,
    max_chars: int,
    task_data: Optional[dict[str, Any]],
) -> str:
    parsed = _extract_first_json_object(text)
    if not isinstance(parsed, dict):
        return ""
    github_inputs = _review_github_inputs(task_data or {})
    summary = _clean_review_text(parsed.get("summary"), max_chars=220)
    if _is_weak_review_text(summary):
        summary = ""
    observation = _clean_review_text(parsed.get("observation"), max_chars=220)
    if _is_weak_review_text(observation):
        observation = ""
    touched_paths = _clean_review_list(parsed.get("touched_paths"), max_items=4, max_chars=120)
    if not touched_paths:
        touched_paths = _clean_review_list(github_inputs.get("touched_paths"), max_items=4, max_chars=120)
    tests_reviewed = _clean_review_list(parsed.get("tests_reviewed"), max_items=3, max_chars=120)
    if not tests_reviewed:
        tests_reviewed = _clean_review_list(github_inputs.get("touched_tests"), max_items=3, max_chars=120)
    verified_identifiers = _clean_review_list(parsed.get("verified_identifiers"), max_items=4, max_chars=80)
    findings_raw = parsed.get("findings")
    findings: list[str] = []
    if isinstance(findings_raw, list):
        for item in findings_raw[:2]:
            if not isinstance(item, dict):
                continue
            issue = _clean_review_text(item.get("issue"), max_chars=140)
            if not issue:
                continue
            rationale = _clean_review_text(item.get("rationale"), max_chars=260)
            combined = f"{issue} {rationale}".strip()
            if _is_weak_review_text(combined):
                continue
            file_name = _clean_review_label(item.get("file"), max_chars=80)
            if file_name:
                findings.append(f"**{issue}** (`{file_name}`): {rationale or 'Check this path.'}")
            else:
                findings.append(f"**{issue}**: {rationale or 'Check this logic.'}")
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
    rendered = "\n\n".join(section for section in sections if section.strip())
    return _finalize_model_reply(rendered, max_chars=max_chars)


@dataclass
class AIAdapter:
    """Real AI adapter using Ollama for provider task processing."""

    model: str = "tinyllama"

    def generate_reply(self, payload: str, task_data: dict[str, Any]) -> str:
        import subprocess

        try:
            prompt = (
                f"{_system_prompt_for_task(task_data, generic_max_chars=300, review_max_chars=1000)}\n\n"
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
            if _task_requires_review_prompt(task_data):
                rendered = _render_structured_review_with_task_data(reply, max_chars=1000, task_data=task_data)
                if rendered:
                    return rendered
                finalized = _finalize_model_reply(reply, max_chars=1000)
                if finalized:
                    return finalized
                return reply[:1000].rstrip() or "[AI adapter] review response was empty"
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
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": _system_prompt_for_task(
                                task_data,
                                generic_max_chars=500,
                                review_max_chars=1000,
                            ),
                        },
                        {"role": "user", "content": payload},
                    ],
                    "max_tokens": 450,
                    "temperature": 0.1,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                message = resp.json()["choices"][0]["message"]
                reply = str(message.get("content") or "").strip()
                if not reply:
                    reply = str(message.get("reasoning_content") or "").strip()
                if _task_requires_review_prompt(task_data):
                    rendered = _render_structured_review_with_task_data(reply, max_chars=1000, task_data=task_data)
                    if rendered:
                        return rendered
                    finalized = _finalize_model_reply(reply, max_chars=1000)
                    if finalized:
                        return finalized
                    return reply[:1000].rstrip() or "[DeepSeek] review response was empty"
                return reply
            return f"[DeepSeek] API error {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            return f"[DeepSeek] error: {exc}"


class WorkspaceManager:
    """Manages autonomous workspace synchronization (Git fetch/checkout)."""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _run_git(self, cwd: str, args: list[str]) -> tuple[int, str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
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

    def build_review_context(
        self,
        workspace_path: str,
        touched_paths: list[str],
        *,
        max_files: int = 3,
        max_chars: int = 2200,
    ) -> str:
        if not workspace_path or not isinstance(touched_paths, list):
            return ""
        sections = [f"Local workspace path: {workspace_path}", "Workspace file excerpts:"]
        remaining = max_chars
        added = 0
        for path in touched_paths:
            if added >= max_files or remaining <= 180:
                break
            resolved = self._resolve_repo_file(workspace_path, str(path or ""))
            if not resolved or not os.path.isfile(resolved):
                continue
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError:
                continue
            content = content.strip()
            if not content:
                continue
            excerpt = content[: min(remaining, 700)].strip()
            block = f"- {path}\n{excerpt}"
            sections.append(block)
            remaining -= len(block) + 2
            added += 1
        if added == 0:
            return ""
        return "\n\n".join(sections)


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
    def _bridge_status_action(interbot_message: Optional[dict[str, Any]]) -> Optional[str]:
        if not isinstance(interbot_message, dict):
            return None
        intent = interbot_message.get("intent")
        intent_type = intent.get("type") if isinstance(intent, dict) else None
        if intent_type == "code.review.approve":
            return "approved"
        if intent_type == "code.review.request":
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
        action = self._bridge_status_action(interbot_message)
        if action:
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

        # Phase 4: Sync workspace if this is a GitHub PR task
        if interbot_message:
            task = interbot_message.get("task") if isinstance(interbot_message.get("task"), dict) else {}
            inputs = task.get("inputs") if isinstance(task.get("inputs"), dict) else {}
            github_inputs = dict(inputs.get("github") or {}) if isinstance(inputs.get("github"), dict) else {}
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
                    workspace_context = await asyncio.to_thread(
                        self.workspace.build_review_context,
                        workspace_path,
                        touched_paths,
                    )
                    if workspace_context:
                        instructions = f"{instructions}\n\nAdditional local workspace context:\n{workspace_context}"
                else:
                    print(f"[mep workspace] sync failed: {path_or_err}")
                    # We continue anyway, but the adapter might be working on stale code

            task_copy = copy.deepcopy(task) if isinstance(task, dict) else {}
            inputs_copy = copy.deepcopy(inputs) if isinstance(inputs, dict) else {}
            inputs_copy["github"] = github_inputs
            if inputs_copy:
                task_copy["inputs"] = inputs_copy
            if task_copy:
                adapter_task_data["task"] = task_copy
            intent = interbot_message.get("intent")
            if isinstance(intent, dict):
                adapter_task_data["intent"] = copy.deepcopy(intent)

        result = self.adapter.generate_reply(instructions, adapter_task_data)
        if self._bridge_eligible_interbot_message(task_data, interbot_message):
            bridged = await self._attempt_live_call_bridge(task_data, interbot_message, result)
            if bridged:
                self._report_bridge_status(
                    interbot_message,
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
                task_id=task_id,
                status="completed",
                detail=result,
            )
            return
        self.complete(task_id, result)
        self._report_bridge_status(
            interbot_message,
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
    parser.add_argument("--adapter", default="mock", choices=["mock", "ollama", "deepseek"], help="Provider adapter.")

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
