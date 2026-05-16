#!/usr/bin/env python3
"""Unified node runtime for fast onboarding (`init`, `up`, `run`, `status`, `doctor`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import requests

try:
    from node.identity import MEPIdentity
except ImportError:  # pragma: no cover - supports direct file execution
    from identity import MEPIdentity


DEFAULT_HUB_URL = os.getenv("HUB_URL", "http://localhost:8000")
DEFAULT_WS_URL = os.getenv("WS_URL", "ws://localhost:8000")


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
    return os.path.join(_default_key_dir(), "mep_runtime.pem")


def _ensure_key_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


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


def _resolve_runtime_alias(key_path: str, cli_alias: Optional[str]) -> str:
    if cli_alias:
        return cli_alias
    persisted = _read_alias_sidecar(key_path)
    if persisted:
        return persisted
    return "mep-runtime"


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


class RuntimeNode:
    def __init__(self, identity: MEPIdentity, hub_url: str, ws_url: str, adapter: MockAdapter, alias: Optional[str] = None):
        self.identity = identity
        self.node_id = identity.node_id
        self.hub_url = hub_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.adapter = adapter
        self.alias = alias
        self.running = True
        self.max_purchase_price = float(os.getenv("MEP_MAX_PURCHASE_PRICE", "0.0"))

    def _auth_headers(self, payload: str) -> dict[str, str]:
        headers = self.identity.get_auth_headers(payload)
        headers["Content-Type"] = "application/json"
        return headers

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

    def heartbeat(self) -> None:
        payload = {"node_id": self.node_id, "availability": "online"}
        payload_str = json.dumps(payload)
        headers = self._auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        code, _body, _raw = _safe_request("POST", f"{self.hub_url}/registry/heartbeat", data=payload_str, headers=headers, timeout=5)
        if code != 200:
            print(f"[mep run] heartbeat failed status={code}")

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

    async def process_task(self, task_data: dict[str, Any]) -> None:
        task_id = str(task_data.get("id") or "")
        payload = str(task_data.get("payload") or "")
        result = self.adapter.generate_reply(payload, task_data)
        self.complete(task_id, result)

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
            await self.process_task(data.get("data", {}))

    async def _recv_loop(self, ws: Any) -> None:
        last_status_at = 0.0
        STATUS_INTERVAL_SECONDS = 60.0
        while self.running:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.ping()
                # Periodic status heartbeat (every 60s)
                now = time.time()
                if now - last_status_at >= STATUS_INTERVAL_SECONDS:
                    code, diag, _ = self._safe_request("GET", f"/diagnostic?node_id={self.node_id}")
                    online = diag.get("ws_connected") if diag else False
                    print(f"[mep run] alive node={self.node_id} ws_connected={'OK' if online else 'NO'}")
                    if not online:
                        print("[mep run] hub reports ws_connected=false — may need reconnect")
                    self.heartbeat()
                    last_status_at = now
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
        reconnect_attempts = 0
        while self.running:
            uri = self._ws_uri()
            try:
                async with ws_connect(uri) as ws:
                    print(f"[mep run] connected ws node={self.node_id}")
                    await self._recv_loop(ws)
            except KeyboardInterrupt:
                self.running = False
            except Exception as exc:  # noqa: BLE001
                reconnect_attempts += 1
                print(f"[mep run] disconnected node={self.node_id} error={exc}")
                print(f"[mep run] reconnecting in 3s (attempt {reconnect_attempts})...")
                await asyncio.sleep(3.0)
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
    print(f"[mep init] node_id={identity.node_id}")
    if identity.generated_new_key:
        print(f"[mep init] generated key={identity.key_path}")
    payload = {
        "pubkey": identity.pub_pem,
        "alias": args.alias,
        "x25519_public_key": identity.x25519_public_key,
    }
    code, body, raw = _safe_request("POST", f"{args.hub_url.rstrip('/')}/register", json_body=payload)
    if code != 200:
        print(f"[mep init] register failed status={code} detail={raw}")
        return 2
    _write_alias_sidecar(args.key_path, args.alias)
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
    if args.adapter != "mock":
        print("[mep run] only adapter=mock is supported in this phase")
        return 2
    _ensure_key_parent(args.key_path)
    identity = MEPIdentity(args.key_path)
    alias = _resolve_runtime_alias(args.key_path, args.alias)
    runtime = RuntimeNode(
        identity=identity,
        hub_url=args.hub_url,
        ws_url=args.ws_url,
        adapter=MockAdapter(),
        alias=alias,
    )
    print(f"[mep run] adapter=mock node_id={identity.node_id} alias={alias}")
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
        help="Path to provider private key (defaults to repo-local .mep/mep_runtime.pem).",
    )
    parser.add_argument("--adapter", default="mock", choices=["mock"], help="Provider adapter.")

    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Generate/load key and register node.")
    init_p.add_argument("--alias", default="mep-runtime", help="Node alias for registration.")
    init_p.set_defaults(func=cmd_init)

    up_p = sub.add_parser("up", help="One-command bootstrap: init + doctor + run.")
    up_p.add_argument("--alias", default="mep-runtime", help="Node alias for registration.")
    up_p.set_defaults(func=cmd_up)

    run_p = sub.add_parser("run", help="Run standardized listener runtime.")
    run_p.add_argument("--alias", default=None, help="Node alias for registration; defaults to persisted alias if present.")
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
        args.key_path = _default_key_path()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
