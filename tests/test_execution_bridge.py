import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from clients.adapters.mep_codex_provider import CodexProvider
from clients.shared.execution_bridge import execute_bridge_command
from clients.shared.mep_client import DEFAULT_EXECUTION_MUST_INCLUDE, EXECUTION_RESULT_TYPE, MEPClient


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.completed_payloads: list[dict] = []

    def post(self, url: str, data: str | None = None, headers: dict | None = None, timeout: int | None = None, json=None):
        if url.endswith("/tasks/complete"):
            self.completed_payloads.append(__import__("json").loads(data or "{}"))
            return _FakeResponse({"status": "success"})
        raise AssertionError(f"unexpected POST url: {url}")


def test_build_execution_request_message_includes_execution_metadata(tmp_path: Path) -> None:
    client = MEPClient(str(tmp_path / "sender.pem"))
    envelope = client.build_execution_request_message(
        "Patch the runtime bridge.",
        "node_target",
        task_inputs={"workspace_path": "/repo", "files": ["clients/adapters/mep_codex_provider.py"]},
        max_runtime_seconds=120,
    )

    assert envelope["intent"]["type"] == "coordination.request"
    assert envelope["conversation"]["turn_type"] == "operator_dm"
    assert envelope["task"]["expected_output"]["result_type"] == EXECUTION_RESULT_TYPE
    assert envelope["task"]["expected_output"]["must_include"] == DEFAULT_EXECUTION_MUST_INCLUDE
    assert envelope["task"]["inputs"]["workspace_path"] == "/repo"
    assert envelope["task"]["constraints"]["required_capabilities"] == ["code_edit"]
    assert envelope["task"]["constraints"]["max_runtime_seconds"] == 120


def test_execute_bridge_command_invokes_local_bridge(tmp_path: Path) -> None:
    script_path = tmp_path / "bridge.py"
    script_path.write_text(
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "payload = {\n"
        "  'execution_started': True,\n"
        "  'workspace_opened': True,\n"
        "  'file_edited': True,\n"
        "  'file_path': request['task']['inputs']['files'][0],\n"
        "  'diff_summary': 'patched requested file',\n"
        "  'branch': 'feat/runtime-bridge',\n"
        "  'commit_sha': 'abc1234',\n"
        "  'pr': '42'\n"
        "}\n"
        "print(json.dumps(payload))\n",
        encoding="utf-8",
    )
    request_payload = {
        "task": {
            "inputs": {
                "files": ["clients/adapters/mep_codex_provider.py"],
            }
        }
    }
    python_exe = sys.executable.replace("\\", "/")
    bridge_cmd = f'"{python_exe}" "{script_path.as_posix()}"'

    result = asyncio.run(execute_bridge_command(request_payload, command=bridge_cmd, timeout_seconds=10))

    assert result["execution_started"] is True
    assert result["workspace_opened"] is True
    assert result["file_edited"] is True
    assert result["file_path"] == "clients/adapters/mep_codex_provider.py"
    assert result["branch"] == "feat/runtime-bridge"


def test_prepare_dm_reply_payload_requires_encryption(monkeypatch, tmp_path: Path) -> None:
    client = MEPClient(str(tmp_path / "sender.pem"))
    client.privacy_mode = "prefer_encrypted"
    monkeypatch.setattr(client, "get_registry_entry", AsyncMock(return_value=None))

    try:
        asyncio.run(client.prepare_dm_reply_payload("reply", "node_peer", require_encrypted=True))
    except ValueError as exc:
        assert "Encrypted DM is required" in str(exc)
    else:
        raise AssertionError("Expected prepare_dm_reply_payload() to fail closed when encryption is required")

    assert client.privacy_mode == "prefer_encrypted"


def test_codex_provider_routes_execution_dm_to_bridge(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEP_PRIVACY_MODE", "plaintext_only")
    monkeypatch.setenv("MEP_KEY_DIR", str(tmp_path / ".mep-home"))
    provider = CodexProvider()
    fake_session = _FakeSession()
    provider.client.session = fake_session
    provider.client.prepare_dm_reply_payload = AsyncMock(side_effect=lambda text, peer_node_id, require_encrypted=False: text)
    llm_reply = AsyncMock(return_value="should not be used")
    provider._generate_reply = llm_reply

    async def _fake_execute_bridge(request_payload, *, command=None, runtime_config=None, timeout_seconds=None):
        return {
            "execution_started": True,
            "workspace_opened": True,
            "file_edited": True,
            "file_path": "scripts/github-actions/verify_checkout_provenance.sh",
            "diff_summary": "normalized origin validation",
            "branch": "feat/runtime-bridge",
            "commit_sha": "deadbeef",
            "pr": "99",
        }

    monkeypatch.setattr("clients.adapters.mep_codex_provider.execute_bridge_command", _fake_execute_bridge)

    inbound = provider.client.build_execution_request_message(
        "Edit the provenance script.",
        provider.client.node_id,
        task_inputs={"files": ["scripts/github-actions/verify_checkout_provenance.sh"]},
    )
    task = {
        "id": "task-1",
        "consumer_id": "node_consumer",
        "payload": json.dumps(inbound),
        "bounty": 0.0,
        "target_node": provider.client.node_id,
    }

    asyncio.run(provider.complete_task(task))

    llm_reply.assert_not_awaited()
    assert len(fake_session.completed_payloads) == 1
    result_payload = fake_session.completed_payloads[0]["result_payload"]
    assert "EXECUTION_STARTED yes." in result_payload
    assert "FILE_EDITED yes." in result_payload
    assert "FILE scripts/github-actions/verify_checkout_provenance.sh." in result_payload
    assert "BRANCH feat/runtime-bridge." in result_payload


def test_codex_provider_uses_persistent_identity_store(monkeypatch, tmp_path: Path) -> None:
    store_dir = tmp_path / ".mep-home"
    monkeypatch.setenv("MEP_KEY_DIR", str(store_dir))
    monkeypatch.delenv("MEP_BOT_KEY_PATH", raising=False)
    monkeypatch.delenv("MEP_MANIFEST_PATH", raising=False)
    monkeypatch.setenv("MEP_ALIAS", "Persistent Codex")

    provider = CodexProvider()

    assert Path(provider.client.identity.key_path).parent == store_dir
    assert provider.client.identity.key_path.endswith(".pem")
    registry = json.loads((store_dir / "bots.json").read_text(encoding="utf-8"))
    assert registry["aliases"]["Persistent Codex"]["key_path"] == provider.client.identity.key_path
    assert os.path.exists(provider.client.identity.key_path)
