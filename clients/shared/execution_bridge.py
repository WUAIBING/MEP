import asyncio
import json
import os
import shlex
from typing import Any, Optional

EXECUTION_RESULT_TYPE = "code_edit_status"
EXECUTION_REQUIRED_CAPABILITIES = {"code_edit", "workspace_edit"}
EXECUTION_ALLOWED_INTENTS = {"coordination.request"}
EXECUTION_ALLOWED_TURN_TYPES = {"operator_dm", "execution_request"}


def is_execution_request(interbot_message: Optional[dict[str, Any]]) -> bool:
    if not isinstance(interbot_message, dict):
        return False
    intent = interbot_message.get("intent")
    intent_type = intent.get("type") if isinstance(intent, dict) else None
    if intent_type not in EXECUTION_ALLOWED_INTENTS:
        return False
    conversation = interbot_message.get("conversation")
    turn_type = conversation.get("turn_type") if isinstance(conversation, dict) else None
    if isinstance(turn_type, str) and turn_type and turn_type not in EXECUTION_ALLOWED_TURN_TYPES:
        return False
    task = interbot_message.get("task")
    if not isinstance(task, dict):
        return False
    expected_output = task.get("expected_output")
    result_type = expected_output.get("result_type") if isinstance(expected_output, dict) else None
    if result_type == EXECUTION_RESULT_TYPE:
        return True
    constraints = task.get("constraints")
    capabilities = constraints.get("required_capabilities") if isinstance(constraints, dict) else None
    if isinstance(capabilities, list):
        normalized = {str(item).strip().lower() for item in capabilities if str(item).strip()}
        if normalized & EXECUTION_REQUIRED_CAPABILITIES:
            return True
    return False


def build_execution_bridge_request(
    interbot_message: dict[str, Any],
    *,
    consumer_id: str,
    task_id: str,
    prompt: str,
) -> dict[str, Any]:
    task = interbot_message.get("task") if isinstance(interbot_message.get("task"), dict) else {}
    source = interbot_message.get("source") if isinstance(interbot_message.get("source"), dict) else {}
    conversation = (
        interbot_message.get("conversation") if isinstance(interbot_message.get("conversation"), dict) else {}
    )
    intent = interbot_message.get("intent") if isinstance(interbot_message.get("intent"), dict) else {}
    return {
        "spec_version": "mep.execution-bridge.v1",
        "task_id": task_id,
        "consumer_id": consumer_id,
        "prompt": prompt,
        "source": {
            "node_id": source.get("node_id"),
            "alias": source.get("alias"),
        },
        "conversation": {
            "context_id": conversation.get("context_id"),
            "reply_to_task_id": conversation.get("reply_to_task_id"),
            "reply_to_message_id": conversation.get("reply_to_message_id"),
            "turn_type": conversation.get("turn_type"),
        },
        "intent": {
            "type": intent.get("type"),
            "priority": intent.get("priority"),
        },
        "task": task,
    }


def build_execution_unavailable_result(reason: str) -> dict[str, Any]:
    return {
        "execution_started": False,
        "workspace_opened": False,
        "file_edited": False,
        "file_path": None,
        "branch": None,
        "commit_sha": None,
        "pr": None,
        "diff_summary": None,
        "status": "execution_unavailable",
        "reason": reason,
    }


def render_execution_result(result: dict[str, Any]) -> str:
    execution_started = "yes" if result.get("execution_started") else "no"
    workspace_opened = "yes" if result.get("workspace_opened") else "no"
    file_edited = "yes" if result.get("file_edited") else "no"
    file_path = result.get("file_path") or "none"
    diff_summary = result.get("diff_summary") or "none"
    branch = result.get("branch") or "none"
    commit_sha = result.get("commit_sha") or "none"
    pr = result.get("pr") or "none"
    reason = result.get("reason")
    parts = [
        f"EXECUTION_STARTED {execution_started}.",
        f"WORKSPACE_OPENED {workspace_opened}.",
        f"FILE_EDITED {file_edited}.",
        f"FILE {file_path}.",
        f"DIFF_SUMMARY {diff_summary}.",
        f"BRANCH {branch}.",
        f"COMMIT {commit_sha}.",
        f"PR {pr}.",
    ]
    if reason:
        parts.append(f"REASON {reason}.")
    return " ".join(parts)


def _resolve_bridge_command(
    explicit_command: Optional[str] = None,
    runtime_config: Optional[dict[str, Any]] = None,
) -> str:
    if explicit_command and explicit_command.strip():
        return explicit_command.strip()
    if isinstance(runtime_config, dict):
        configured = runtime_config.get("execution_bridge_command")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    return os.getenv("MEP_EXECUTION_BRIDGE_COMMAND", "").strip()


async def execute_bridge_command(
    request_payload: dict[str, Any],
    *,
    command: Optional[str] = None,
    runtime_config: Optional[dict[str, Any]] = None,
    timeout_seconds: Optional[int] = None,
) -> dict[str, Any]:
    resolved_command = _resolve_bridge_command(command, runtime_config)
    if not resolved_command:
        return build_execution_unavailable_result("no_runtime_edit_path_configured")
    timeout = timeout_seconds
    if timeout is None and isinstance(runtime_config, dict):
        configured_timeout = runtime_config.get("execution_bridge_timeout_seconds")
        if configured_timeout is not None:
            timeout = int(configured_timeout)
    if timeout is None:
        timeout = int(os.getenv("MEP_EXECUTION_BRIDGE_TIMEOUT_SECONDS", "120"))
    proc = await asyncio.create_subprocess_exec(
        *shlex.split(resolved_command),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request_bytes = json.dumps(request_payload).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(request_bytes), timeout=max(1, timeout))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return build_execution_unavailable_result("execution_bridge_timeout")
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or f"bridge_exit_{proc.returncode}"
        return build_execution_unavailable_result(detail)
    output = stdout.decode("utf-8", errors="replace").strip()
    if not output:
        return build_execution_unavailable_result("execution_bridge_empty_output")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return build_execution_unavailable_result("execution_bridge_non_json_output")
    if not isinstance(parsed, dict):
        return build_execution_unavailable_result("execution_bridge_invalid_payload")
    return parsed
