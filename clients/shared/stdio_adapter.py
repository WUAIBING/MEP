import asyncio
import json
import os
import shlex
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from clients.shared.commands import parse_task_args
from clients.shared.mep_client import MEPClient

DEFAULT_BOUNTY = float(os.getenv("MEP_DEFAULT_BOUNTY", "5.0"))


class StdioAdapter:
    def __init__(self, platform_name: str, default_model: str, key_file_name: str):
        key_path = os.getenv("MEP_BOT_KEY_PATH", os.path.join(tempfile.gettempdir(), key_file_name))
        self.platform_name = platform_name
        self.default_model = default_model
        self.client = MEPClient(key_path)
        self.alias = os.getenv("MEP_ALIAS", platform_name)
        self._recent_interbot_results: dict[str, dict[str, Any]] = {}
        self.live_call_enabled = os.getenv("MEP_LIVE_CALL_ENABLED", "0") not in ("0", "false", "False", "")
        self.call_auto_accept = os.getenv("MEP_CALL_AUTO_ACCEPT", "0") not in ("0", "false", "False", "")
        self.noninteractive_keepalive = os.getenv("MEP_NONINTERACTIVE_KEEPALIVE", "0") not in (
            "0",
            "false",
            "False",
            "",
        )
        self._call_seq_by_context: dict[str, int] = {}

    async def _announce_registry(self) -> None:
        body = {
            "alias": self.alias,
            "skills": ["dm", "chat", self.platform_name.lower()],
            "models": [self.default_model],
            "metadata": {
                "platform": self.platform_name.lower(),
                **self.client.get_privacy_registry_metadata(),
            },
            "availability": "online",
        }
        payload = json.dumps(body)
        response = await asyncio.to_thread(
            self.client.session.post,
            f"{self.client.hub_url.rstrip('/')}/registry/update",
            data=payload,
            headers=self.client._auth_headers(payload),
            timeout=15,
        )
        if response.status_code != 200:
            raise RuntimeError(f"registry update failed: {response.text}")

    async def _handle_result(self, data: dict) -> None:
        task_id = data.get("task_id")
        result = data.get("result_payload", "")
        print(f"[{self.platform_name}] task_result {task_id}: {result}")
        if isinstance(task_id, str) and isinstance(result, str):
            parsed = self.client.parse_interbot_payload(result)
            if parsed:
                self._remember_interbot_result(task_id, result, parsed)
                context_id = None
                conversation = parsed.get("conversation")
                if isinstance(conversation, dict) and isinstance(conversation.get("context_id"), str):
                    context_id = conversation["context_id"]
                print(
                    f"[{self.platform_name}] stored structured dm result {task_id}"
                    + (f" context={context_id}" if context_id else "")
                )

    async def _send_live_call_event(self, payload: dict[str, Any], *, action: str) -> bool:
        ok = await self.client.send_ws_event(payload)
        if not ok:
            print(f"[{self.platform_name}] {action} failed: live socket is not connected")
            return False
        return True
    async def _handle_call_event(self, data: dict[str, Any]) -> None:
        event = str(data.get("event") or "")
        context_id = data.get("context_id") if isinstance(data.get("context_id"), str) else None
        if event == "call.ping":
            if context_id:
                await self._send_live_call_event({"event": "call.pong", "context_id": context_id}, action="call.pong")
            return
        if event == "call.incoming":
            caller = data.get("caller") if isinstance(data.get("caller"), str) else None
            if self.live_call_enabled and self.call_auto_accept and context_id:
                sent = await self._send_live_call_event({"event": "call.accept", "context_id": context_id}, action="call.accept")
                if sent:
                    print(f"[{self.platform_name}] auto-accepted live call context={context_id} caller={caller}")
                return
            print(
                f"[{self.platform_name}] incoming live call context={context_id} caller={caller} "
                f"(use: mepcallaccept {context_id} | mepcalldecline {context_id} <reason>)"
            )
            return
        if event == "call.accepted":
            print(f"[{self.platform_name}] live call accepted context={context_id}")
            return
        if event in {"call.declined", "call.timeout", "call.rejected", "call.cancelled"}:
            print(
                f"[{self.platform_name}] live call {event.removeprefix('call.')} "
                f"context={context_id} reason={data.get('reason')}"
            )
            return
        if event == "call.frame":
            sender = data.get("sender") if isinstance(data.get("sender"), str) else "unknown"
            payload = str(data.get("payload") or "")
            print(f"[{self.platform_name}] live frame context={context_id} sender={sender}: {payload}")
            return
        if event in {"call.hangup", "call.suspended", "call.resumed"}:
            print(f"[{self.platform_name}] {event} context={context_id} detail={data}")

    async def _handle_event(self, data: dict[str, Any]) -> None:
        event = str(data.get("event") or "")
        if event.startswith("call."):
            await self._handle_call_event(data)

    def _remember_interbot_result(self, task_id: str, payload_text: str, message: dict[str, Any]) -> None:
        self._recent_interbot_results[task_id] = {
            "payload_text": payload_text,
            "message": message,
        }
        while len(self._recent_interbot_results) > 20:
            oldest_task_id = next(iter(self._recent_interbot_results))
            del self._recent_interbot_results[oldest_task_id]

    def _format_structured_dm_result_snapshot(self, task_id: str, inbound: dict[str, Any]) -> dict[str, Any]:
        message = inbound.get("message", {})
        source = message.get("source") if isinstance(message, dict) else None
        conversation = message.get("conversation") if isinstance(message, dict) else None
        intent = message.get("intent") if isinstance(message, dict) else None
        return {
            "task_id": task_id,
            "context_id": conversation.get("context_id") if isinstance(conversation, dict) else None,
            "message_id": message.get("message_id") if isinstance(message, dict) else None,
            "source_node_id": source.get("node_id") if isinstance(source, dict) else None,
            "turn_type": conversation.get("turn_type") if isinstance(conversation, dict) else None,
            "intent_type": intent.get("type") if isinstance(intent, dict) else None,
            "payload_text": inbound.get("payload_text"),
            "message": message if isinstance(message, dict) else None,
        }

    @staticmethod
    def _sanitize_snapshot_name_component(value: Optional[str], fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        sanitized = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value.strip())
        sanitized = sanitized.strip("-")
        return sanitized or fallback

    @staticmethod
    def _resolve_snapshot_output_path(output_path: str) -> str:
        base_dir = os.path.abspath(os.getcwd())
        candidate_path = os.path.abspath(output_path)
        try:
            common_path = os.path.commonpath([base_dir, candidate_path])
        except ValueError as exc:
            raise ValueError("--out must stay within the current working directory") from exc
        if common_path != base_dir:
            raise ValueError("--out must stay within the current working directory")
        return candidate_path

    def _parse_structured_dm_cache_options(
        self,
        text: str,
        *,
        command_name: str,
        allowed_options: set[str],
    ) -> Optional[dict[str, Any]]:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] {command_name} parse error: {exc}")
            return None

        options: dict[str, Any] = {
            "context_filter": None,
            "limit": None,
            "emit_json": False,
        }
        i = 0
        while i < len(parts):
            token = parts[i]
            if token == "--context":
                if "--context" not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return None
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return None
                options["context_filter"] = parts[i + 1]
                i += 2
                continue
            if token == "--limit":
                if "--limit" not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return None
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return None
                try:
                    limit = int(parts[i + 1])
                except ValueError:
                    print(f"[{self.platform_name}] --limit must be an integer")
                    return None
                if limit <= 0:
                    print(f"[{self.platform_name}] --limit must be a positive integer")
                    return None
                options["limit"] = limit
                i += 2
                continue
            if token == "--json":
                if "--json" not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return None
                options["emit_json"] = True
                i += 1
                continue
            if token == "--label":
                if "--label" not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return None
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return None
                options["label"] = parts[i + 1]
                i += 2
                continue
            if token == "--out":
                if "--out" not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return None
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return None
                options["out"] = parts[i + 1]
                i += 2
                continue
            print(f"[{self.platform_name}] unknown option {token}")
            return None
        return options

    def _select_recent_structured_dm_entries(
        self,
        *,
        context_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        entries = list(reversed(list(self._recent_interbot_results.items())))
        if context_filter is not None:
            filtered_entries: list[tuple[str, dict[str, Any]]] = []
            for task_id, inbound in entries:
                message = inbound.get("message", {})
                conversation = message.get("conversation") if isinstance(message, dict) else None
                if isinstance(conversation, dict) and conversation.get("context_id") == context_filter:
                    filtered_entries.append((task_id, inbound))
            entries = filtered_entries
        if limit is not None:
            entries = entries[:limit]
        return entries

    def _build_structured_dm_snapshot(
        self,
        *,
        context_filter: Optional[str] = None,
        limit: Optional[int] = None,
        entries: Optional[list[tuple[str, dict[str, Any]]]] = None,
        label: Optional[str] = None,
    ) -> dict[str, Any]:
        selected_entries = (
            entries
            if entries is not None
            else self._select_recent_structured_dm_entries(context_filter=context_filter, limit=limit)
        )
        snapshot = {
            "platform": self.platform_name,
            "context_filter": context_filter,
            "limit": limit,
            "count": len(selected_entries),
            "results": [
                self._format_structured_dm_result_snapshot(task_id, inbound)
                for task_id, inbound in selected_entries
            ],
        }
        if label is not None:
            snapshot["snapshot_label"] = label
            snapshot["captured_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return snapshot

    def _list_recent_structured_dm_results(self, text: str = "") -> None:
        options = self._parse_structured_dm_cache_options(
            text,
            command_name="mepdmlist",
            allowed_options={"--context", "--limit", "--json"},
        )
        if not options:
            return
        context_filter = options["context_filter"]
        limit = options["limit"]
        emit_json = options["emit_json"]

        entries = self._select_recent_structured_dm_entries(context_filter=context_filter, limit=limit)

        if emit_json:
            print(json.dumps(self._build_structured_dm_snapshot(context_filter=context_filter, limit=limit, entries=entries), indent=2))
            return

        if not entries:
            if context_filter is not None:
                print(f"[{self.platform_name}] no stored structured dm results for context={context_filter}")
                return
            print(f"[{self.platform_name}] no stored structured dm results")
            return

        header = f"[{self.platform_name}] recent structured dm results:"
        if context_filter is not None:
            header = f"[{self.platform_name}] recent structured dm results for context={context_filter}:"
        print(header)
        for task_id, inbound in entries:
            message = inbound.get("message", {})
            source = message.get("source") if isinstance(message, dict) else None
            conversation = message.get("conversation") if isinstance(message, dict) else None
            intent = message.get("intent") if isinstance(message, dict) else None
            print(
                f"[{self.platform_name}] - task_id={task_id} "
                f"context_id={conversation.get('context_id') if isinstance(conversation, dict) else None} "
                f"message_id={message.get('message_id') if isinstance(message, dict) else None} "
                f"source={source.get('node_id') if isinstance(source, dict) else None} "
                f"turn_type={conversation.get('turn_type') if isinstance(conversation, dict) else None} "
                f"intent={intent.get('type') if isinstance(intent, dict) else None}"
            )

    def _write_structured_dm_snapshot(self, text: str = "") -> None:
        usage = (
            f"[{self.platform_name}] usage: mepdmsnapshot --label <label> "
            "[--context <context_id>] [--limit <count>] [--out <file>]"
        )
        options = self._parse_structured_dm_cache_options(
            text,
            command_name="mepdmsnapshot",
            allowed_options={"--context", "--limit", "--label", "--out"},
        )
        if not options:
            return
        label = options.get("label")
        if not isinstance(label, str) or not label.strip():
            print(usage)
            return
        context_filter = options["context_filter"]
        limit = options["limit"]
        entries = self._select_recent_structured_dm_entries(context_filter=context_filter, limit=limit)
        snapshot = self._build_structured_dm_snapshot(
            context_filter=context_filter,
            limit=limit,
            entries=entries,
            label=label.strip(),
        )
        output_path = options.get("out")
        if not isinstance(output_path, str) or not output_path.strip():
            safe_context = self._sanitize_snapshot_name_component(context_filter, "all")
            safe_label = self._sanitize_snapshot_name_component(label, "snapshot")
            output_path = f"soak-{safe_context}-{safe_label}.json"
        try:
            output_path = self._resolve_snapshot_output_path(output_path)
        except ValueError as exc:
            print(f"[{self.platform_name}] {exc}")
            return
        try:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            with open(output_path, "w", encoding="utf-8", newline="\n") as snapshot_file:
                json.dump(snapshot, snapshot_file, indent=2)
                snapshot_file.write("\n")
        except OSError as exc:
            print(f"[{self.platform_name}] mepdmsnapshot write error: {exc}")
            return
        print(
            f"[{self.platform_name}] wrote structured dm snapshot {output_path} "
            f"label={label.strip()} count={snapshot['count']}"
            + (f" context={context_filter}" if context_filter else "")
        )

    def _get_stored_structured_dm_context(
        self, task_id: str
    ) -> Optional[tuple[dict[str, Any], dict[str, Any], str, str, Optional[str]]]:
        inbound = self._recent_interbot_results.get(task_id)
        if not inbound:
            print(f"[{self.platform_name}] no stored structured dm result for task {task_id}")
            return None

        message = inbound["message"]
        source = message.get("source") if isinstance(message, dict) else None
        conversation = message.get("conversation") if isinstance(message, dict) else None
        target_node = source.get("node_id") if isinstance(source, dict) else None
        context_id = conversation.get("context_id") if isinstance(conversation, dict) else None
        reply_to_message_id = message.get("message_id") if isinstance(message, dict) else None

        if not isinstance(source, dict):
            print(f"[{self.platform_name}] stored structured dm result {task_id} is missing source")
            return None
        if not isinstance(target_node, str) or not target_node:
            print(f"[{self.platform_name}] stored structured dm result {task_id} is missing source.node_id")
            return None
        if not isinstance(context_id, str) or not context_id:
            print(f"[{self.platform_name}] stored structured dm result {task_id} is missing conversation.context_id")
            return None

        return (
            message,
            source,
            target_node,
            context_id,
            reply_to_message_id if isinstance(reply_to_message_id, str) else None,
        )

    def _get_latest_stored_task_id_for_context(self, context_id: str) -> Optional[str]:
        for task_id, inbound in reversed(list(self._recent_interbot_results.items())):
            message = inbound.get("message", {})
            conversation = message.get("conversation") if isinstance(message, dict) else None
            if isinstance(conversation, dict) and conversation.get("context_id") == context_id:
                return task_id
        print(f"[{self.platform_name}] no stored structured dm results for context={context_id}")
        return None

    def _resolve_stored_task_id_selector(self, parts: list[str], usage: str) -> Optional[tuple[str, list[str]]]:
        if not parts:
            print(usage)
            return None
        if parts[0] != "--context":
            return parts[0], parts[1:]
        if len(parts) < 2:
            print(f"[{self.platform_name}] missing value for --context")
            return None
        task_id = self._get_latest_stored_task_id_for_context(parts[1])
        if not task_id:
            return None
        return task_id, parts[2:]

    def _derive_next_turn_index(self, task_id: str, message: dict[str, Any], *, require: bool = False) -> Optional[int]:
        conversation = message.get("conversation") if isinstance(message, dict) else None
        if not isinstance(conversation, dict):
            if require:
                print(f"[{self.platform_name}] stored structured dm result {task_id} is missing conversation")
            return None
        turn_index = conversation.get("turn_index")
        if turn_index is None:
            if require:
                print(
                    f"[{self.platform_name}] stored structured dm result {task_id} is missing conversation.turn_index; "
                    "pass an explicit next_turn_index"
                )
            return None
        if not isinstance(turn_index, int) or turn_index < 1:
            if require:
                print(f"[{self.platform_name}] stored structured dm result {task_id} has invalid conversation.turn_index")
            return None
        return turn_index + 1

    async def _submit(self, text: str) -> None:
        payload, bounty, model, target = parse_task_args(text, DEFAULT_BOUNTY, self.default_model)
        if not payload:
            print(f"[{self.platform_name}] usage: mep <task> [--bounty 5.0] [--model model] [--target node_id]")
            return
        response = await self.client.submit_task(payload, bounty, model, target)
        data = response["json"]
        if response["status_code"] != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] submit failed: {data}")
            return
        print(f"[{self.platform_name}] submitted task {data.get('task_id')}")

    async def _send_dm(self, target_node: str, message: str) -> None:
        response = await self.client.submit_task(message, 0.0, None, target_node)
        data = response["json"]
        if response["status_code"] != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] dm failed: {data}")
            return
        print(f"[{self.platform_name}] sent dm task {data.get('task_id')} to {target_node}")

    async def _start_live_call(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepcall parse error: {exc}")
            return
        if not parts:
            print(
                f"[{self.platform_name}] usage: mepcall <node_id> "
                "[--context <context_id>] [--timeout-ms <ms>] [--grace-ms <ms>]"
            )
            return
        target_node = parts[0]
        context_id = str(uuid.uuid4())
        timeout_ms = 30000
        grace_ms = 10000
        i = 1
        while i < len(parts):
            token = parts[i]
            if token == "--context":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                context_id = parts[i + 1]
                i += 2
                continue
            if token == "--timeout-ms":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                try:
                    timeout_ms = int(parts[i + 1])
                except ValueError:
                    print(f"[{self.platform_name}] --timeout-ms must be an integer")
                    return
                i += 2
                continue
            if token == "--grace-ms":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                try:
                    grace_ms = int(parts[i + 1])
                except ValueError:
                    print(f"[{self.platform_name}] --grace-ms must be an integer")
                    return
                i += 2
                continue
            print(f"[{self.platform_name}] unknown option {token}")
            return
        sent = await self._send_live_call_event(
            {
                "event": "call.invite",
                "context_id": context_id,
                "callee": target_node,
                "timeout_ms": timeout_ms,
                "reconnect_grace_ms": grace_ms,
            },
            action="call.invite",
        )
        if sent:
            self._call_seq_by_context.setdefault(context_id, 0)
            print(f"[{self.platform_name}] live call invite sent context={context_id} callee={target_node}")

    async def _accept_live_call(self, text: str) -> None:
        context_id = text.strip()
        if not context_id:
            print(f"[{self.platform_name}] usage: mepcallaccept <context_id>")
            return
        sent = await self._send_live_call_event({"event": "call.accept", "context_id": context_id}, action="call.accept")
        if sent:
            self._call_seq_by_context.setdefault(context_id, 0)
            print(f"[{self.platform_name}] live call accepted context={context_id}")

    async def _decline_live_call(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepcalldecline parse error: {exc}")
            return
        if not parts:
            print(f"[{self.platform_name}] usage: mepcalldecline <context_id> [reason]")
            return
        context_id = parts[0]
        reason = "declined"
        if len(parts) > 1:
            reason = " ".join(parts[1:]).strip() or reason
        sent = await self._send_live_call_event(
            {"event": "call.decline", "context_id": context_id, "reason": reason},
            action="call.decline",
        )
        if sent:
            print(f"[{self.platform_name}] live call declined context={context_id} reason={reason}")

    async def _send_live_call_frame(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepcallframe parse error: {exc}")
            return
        if len(parts) < 2:
            print(f"[{self.platform_name}] usage: mepcallframe <context_id> <message> [--seq <n>]")
            return
        context_id = parts[0]
        seq = None
        message_parts: list[str] = []
        i = 1
        while i < len(parts):
            token = parts[i]
            if token == "--seq":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                try:
                    seq = int(parts[i + 1])
                except ValueError:
                    print(f"[{self.platform_name}] --seq must be an integer")
                    return
                i += 2
                continue
            message_parts.append(token)
            i += 1
        payload = " ".join(message_parts).strip()
        if not payload:
            print(f"[{self.platform_name}] usage: mepcallframe <context_id> <message> [--seq <n>]")
            return
        if seq is None:
            seq = self._call_seq_by_context.get(context_id, 0)
        sent = await self._send_live_call_event(
            {"event": "call.frame", "context_id": context_id, "seq": seq, "content_type": "text/plain", "payload": payload},
            action="call.frame",
        )
        if sent:
            self._call_seq_by_context[context_id] = seq + 1
            print(f"[{self.platform_name}] live frame sent context={context_id} seq={seq}")

    async def _hangup_live_call(self, text: str) -> None:
        context_id = text.strip()
        if not context_id:
            print(f"[{self.platform_name}] usage: mepcallhangup <context_id>")
            return
        sent = await self._send_live_call_event({"event": "call.hangup", "context_id": context_id}, action="call.hangup")
        if sent:
            print(f"[{self.platform_name}] live call hangup sent context={context_id}")

    async def _send_structured_dm(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepdmx parse error: {exc}")
            return
        if len(parts) < 2:
            print(
                f"[{self.platform_name}] usage: mepdmx <node_id> <message> "
                "[--context id] [--reply-task id] [--reply-message id] [--turn-type type] "
                "[--intent type] [--priority <level>] [--max-turns count] "
                "[--max-duration-seconds seconds] [--checkpoint-interval count]"
            )
            return

        target_node = parts[0]
        allowed_options = {
            "--context",
            "--reply-task",
            "--reply-message",
            "--turn-type",
            "--intent",
            "--priority",
            "--max-turns",
            "--max-duration-seconds",
            "--checkpoint-interval",
        }
        options: dict[str, str] = {}
        message_parts: list[str] = []
        i = 1
        while i < len(parts):
            token = parts[i]
            if token.startswith("--"):
                if token not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                options[token] = parts[i + 1]
                i += 2
                continue
            message_parts.append(token)
            i += 1

        message = " ".join(message_parts).strip()
        if not message:
            print(f"[{self.platform_name}] usage: mepdmx <node_id> <message> [options]")
            return

        try:
            max_turns = int(options["--max-turns"]) if "--max-turns" in options else None
            max_duration_seconds = (
                int(options["--max-duration-seconds"])
                if "--max-duration-seconds" in options
                else None
            )
            checkpoint_interval = (
                int(options["--checkpoint-interval"])
                if "--checkpoint-interval" in options
                else None
            )
            session_safety = None
            if (
                max_turns is not None
                or max_duration_seconds is not None
                or checkpoint_interval is not None
            ):
                session_safety = self.client.build_session_safety_metadata(
                    max_turns=max_turns,
                    max_duration_seconds=max_duration_seconds,
                    checkpoint_interval=checkpoint_interval,
                )
            response = await self.client.submit_dm(
                message,
                target_node,
                context_id=options.get("--context"),
                reply_to_task_id=options.get("--reply-task"),
                reply_to_message_id=options.get("--reply-message"),
                turn_type=options.get("--turn-type", "chat_turn"),
                intent_type=options.get("--intent", "chat.request"),
                priority=options.get("--priority", "normal"),
                session_safety=session_safety,
                turn_index=1,
            )
        except ValueError as exc:
            print(f"[{self.platform_name}] threaded dm error: {exc}")
            return
        data = response["json"]
        if response["status_code"] != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] dm failed: {data}")
            return
        print(
            f"[{self.platform_name}] sent threaded dm task {data.get('task_id')} "
            f"to {target_node} context={response.get('context_id')}"
        )

    async def _send_safe_dm_reply(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepdmreplysafe parse error: {exc}")
            return
        usage = (
            f"[{self.platform_name}] usage: mepdmreplysafe <task_id> <next_turn_index> <reply> "
            "[--checkpoint-summary text] [--turn-type type] [--intent type] [--priority <level>] [--human-note text] "
            "or mepdmreplysafe --context <context_id> <next_turn_index|auto> <reply> [options]"
        )
        resolved = self._resolve_stored_task_id_selector(parts, usage)
        if not resolved:
            return
        task_id, parts = resolved
        if len(parts) < 2:
            print(usage)
            return
        options: dict[str, str] = {}
        reply_parts: list[str] = []
        allowed_options = {
            "--checkpoint-summary",
            "--turn-type",
            "--intent",
            "--priority",
            "--human-note",
        }
        i = 1
        while i < len(parts):
            token = parts[i]
            if token.startswith("--"):
                if token not in allowed_options:
                    print(f"[{self.platform_name}] unknown option {token}")
                    return
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                options[token] = parts[i + 1]
                i += 2
                continue
            reply_parts.append(token)
            i += 1

        reply_text = " ".join(reply_parts).strip()
        if not reply_text:
            print(usage)
            return

        stored = self._get_stored_structured_dm_context(task_id)
        if not stored:
            return
        message, _source, _target_node, _context_id, _reply_to_message_id = stored
        if parts[0] == "auto":
            next_turn_index = self._derive_next_turn_index(task_id, message, require=True)
            if next_turn_index is None:
                return
        else:
            try:
                next_turn_index = int(parts[0])
            except ValueError:
                print(f"[{self.platform_name}] next_turn_index must be an integer")
                return

        try:
            response = await self.client.submit_safe_dm_reply(
                reply_text,
                message,
                next_turn_index=next_turn_index,
                checkpoint_summary=options.get("--checkpoint-summary"),
                inbound_task_id=task_id,
                turn_type=options.get("--turn-type"),
                intent_type=options.get("--intent"),
                priority=options.get("--priority"),
                human_note=options.get("--human-note"),
            )
        except ValueError as exc:
            print(f"[{self.platform_name}] safe dm reply error: {exc}")
            return

        action = response.get("reply_action")
        if action == "stop":
            print(
                f"[{self.platform_name}] safe reply stopped for {task_id}: "
                f"{', '.join(response.get('safety', {}).get('violations', [])) or 'limits exceeded'}"
            )
            return

        data = response.get("json", {})
        if response.get("status_code") != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] safe dm reply failed: {data}")
            return

        action_label = {
            "reply": "safe reply task",
            "checkpoint": "safe checkpoint task",
        }.get(action, f"safe reply {action} task")
        print(
            f"[{self.platform_name}] {action_label} {data.get('task_id')} "
            f"context={response.get('context_id')}"
        )

    async def _send_human_approval_request_dm(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepdmhumanapproval parse error: {exc}")
            return
        usage = (
            f"[{self.platform_name}] usage: mepdmhumanapproval <task_id> <summary> "
            "[--decision-type type] [--review-decision verdict] "
            "[--blocker text] [--next-action text] [--priority <level>] "
            "[--target-node node_id] [--target-alias alias] [--human-note text] "
            "or mepdmhumanapproval --context <context_id> <summary> [options]"
        )
        resolved = self._resolve_stored_task_id_selector(parts, usage)
        if not resolved:
            return
        task_id, parts = resolved
        if not parts:
            print(usage)
            return
        summary_parts: list[str] = []
        decision_type = "merge_decision"
        review_decision: Optional[str] = None
        blockers: list[str] = []
        next_action: Optional[str] = None
        priority = "high"
        target_node_override: Optional[str] = None
        target_alias_override: Optional[str] = None
        human_note: Optional[str] = None
        i = 0
        while i < len(parts):
            token = parts[i]
            if not token.startswith("--"):
                summary_parts.append(token)
                i += 1
                continue
            if token == "--decision-type":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                decision_type = parts[i + 1]
                i += 2
                continue
            if token == "--review-decision":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                review_decision = parts[i + 1]
                i += 2
                continue
            if token == "--blocker":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                blockers.append(parts[i + 1])
                i += 2
                continue
            if token == "--next-action":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                next_action = parts[i + 1]
                i += 2
                continue
            if token == "--priority":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                priority = parts[i + 1]
                i += 2
                continue
            if token == "--target-node":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                target_node_override = parts[i + 1]
                i += 2
                continue
            if token == "--target-alias":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                target_alias_override = parts[i + 1]
                i += 2
                continue
            if token == "--human-note":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                human_note = parts[i + 1]
                i += 2
                continue
            print(f"[{self.platform_name}] unknown option {token}")
            return

        summary = " ".join(summary_parts).strip()
        if not summary:
            print(usage)
            return

        stored = self._get_stored_structured_dm_context(task_id)
        if not stored:
            return
        _message, source, cached_target_node, context_id, reply_to_message_id = stored
        turn_index = self._derive_next_turn_index(task_id, _message)
        target_node = target_node_override or cached_target_node
        target_alias = target_alias_override
        if target_alias is None and not target_node_override and isinstance(source.get("alias"), str):
            target_alias = source.get("alias")

        try:
            request_kwargs = {
                "context_id": context_id,
                "decision_type": decision_type,
                "target_alias": target_alias,
                "reply_to_task_id": task_id,
                "reply_to_message_id": reply_to_message_id,
                "review_decision": review_decision,
                "blockers": blockers or None,
                "recommended_next_action": next_action,
                "priority": priority,
                "human_note": human_note,
            }
            if turn_index is not None:
                request_kwargs["turn_index"] = turn_index
            response = await self.client.submit_human_approval_request_dm(
                summary,
                target_node,
                **request_kwargs,
            )
        except ValueError as exc:
            print(f"[{self.platform_name}] human approval request error: {exc}")
            return

        data = response.get("json", {})
        if response.get("status_code") != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] human approval request failed: {data}")
            return

        print(
            f"[{self.platform_name}] human approval request sent task {data.get('task_id')} "
            f"context={response.get('context_id')}"
        )

    async def _send_review_verdict_dm(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            print(f"[{self.platform_name}] mepdmverdict parse error: {exc}")
            return
        usage = (
            f"[{self.platform_name}] usage: mepdmverdict <task_id> <verdict> <rationale> "
            "[--condition text] [--recommendation text] [--priority <level>] [--human-note text] "
            "or mepdmverdict --context <context_id> <verdict> <rationale> [options]"
        )
        resolved = self._resolve_stored_task_id_selector(parts, usage)
        if not resolved:
            return
        task_id, parts = resolved
        if len(parts) < 2:
            print(usage)
            return
        verdict = parts[0]
        rationale_parts: list[str] = []
        conditions: list[str] = []
        recommendation: Optional[str] = None
        priority = "normal"
        human_note: Optional[str] = None
        i = 1
        while i < len(parts):
            token = parts[i]
            if not token.startswith("--"):
                rationale_parts.append(token)
                i += 1
                continue
            if token == "--condition":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                conditions.append(parts[i + 1])
                i += 2
                continue
            if token == "--recommendation":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                recommendation = parts[i + 1]
                i += 2
                continue
            if token == "--priority":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                priority = parts[i + 1]
                i += 2
                continue
            if token == "--human-note":
                if i + 1 >= len(parts):
                    print(f"[{self.platform_name}] missing value for {token}")
                    return
                human_note = parts[i + 1]
                i += 2
                continue
            print(f"[{self.platform_name}] unknown option {token}")
            return

        rationale = " ".join(rationale_parts).strip()
        if not rationale:
            print(usage)
            return

        stored = self._get_stored_structured_dm_context(task_id)
        if not stored:
            return
        _message, source, target_node, context_id, reply_to_message_id = stored
        turn_index = self._derive_next_turn_index(task_id, _message)

        try:
            request_kwargs = {
                "context_id": context_id,
                "target_alias": source.get("alias")
                if isinstance(source, dict) and isinstance(source.get("alias"), str)
                else None,
                "reply_to_task_id": task_id,
                "reply_to_message_id": reply_to_message_id if isinstance(reply_to_message_id, str) else None,
                "conditions": conditions or None,
                "human_recommendation": recommendation,
                "priority": priority,
                "human_note": human_note,
            }
            if turn_index is not None:
                request_kwargs["turn_index"] = turn_index
            response = await self.client.submit_review_verdict_dm(
                verdict,
                rationale,
                target_node,
                **request_kwargs,
            )
        except ValueError as exc:
            print(f"[{self.platform_name}] review verdict error: {exc}")
            return

        data = response.get("json", {})
        if response.get("status_code") != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] review verdict failed: {data}")
            return

        print(
            f"[{self.platform_name}] review verdict sent task {data.get('task_id')} "
            f"context={response.get('context_id')}"
        )

    async def _offer_data(self, price: str, payload: str) -> None:
        bounty = -abs(float(price))
        response = await self.client.submit_task("Data offer available", bounty, secret_data=payload)
        data = response["json"]
        if response["status_code"] != 200 or data.get("status") != "success":
            print(f"[{self.platform_name}] data offer failed: {data}")
            return
        print(f"[{self.platform_name}] offered data task {data.get('task_id')} for {bounty} SECONDS")

    async def _cancel(self, task_id: str) -> None:
        response = await self.client.cancel_task(task_id)
        data = response["json"]
        if response["status_code"] != 200:
            print(f"[{self.platform_name}] cancel failed: {data}")
            return
        print(f"[{self.platform_name}] cancelled task {task_id}")

    async def _result(self, task_id: str) -> None:
        response = await self.client.get_result(task_id)
        data = response["json"]
        if response["status_code"] != 200:
            print(f"[{self.platform_name}] result lookup failed: {data}")
            return
        print(f"[{self.platform_name}] result for {task_id}: {data.get('result_payload')}")

    async def _balance(self) -> None:
        response = await self.client.get_balance()
        data = response["json"]
        if response["status_code"] != 200:
            print(f"[{self.platform_name}] balance lookup failed: {data}")
            return
        print(f"[{self.platform_name}] balance for {self.client.node_id}: {data.get('balance_seconds')} SECONDS")

    async def _dispatch_line(self, line: str) -> bool:
        text = line.strip()
        if not text:
            return True
        if text in {"quit", "exit"}:
            return False
        if text.startswith("mepdm "):
            parts = text.split(" ", 2)
            if len(parts) < 3:
                print(f"[{self.platform_name}] usage: mepdm <node_id> <message>")
                return True
            await self._send_dm(parts[1], parts[2])
            return True
        if text.startswith("mepcall "):
            await self._start_live_call(text[8:])
            return True
        if text.startswith("mepcallaccept "):
            await self._accept_live_call(text[14:])
            return True
        if text.startswith("mepcalldecline "):
            await self._decline_live_call(text[15:])
            return True
        if text.startswith("mepcallframe "):
            await self._send_live_call_frame(text[13:])
            return True
        if text.startswith("mepcallhangup "):
            await self._hangup_live_call(text[14:])
            return True
        if text.startswith("mepdmx "):
            await self._send_structured_dm(text[7:])
            return True
        if text == "mepdmlist":
            self._list_recent_structured_dm_results()
            return True
        if text.startswith("mepdmlist "):
            self._list_recent_structured_dm_results(text[10:])
            return True
        if text.startswith("mepdmsnapshot"):
            snapshot_args = text[14:].strip() if len(text) > 14 else ""
            self._write_structured_dm_snapshot(snapshot_args)
            return True
        if text.startswith("mepdmhumanapproval "):
            await self._send_human_approval_request_dm(text[19:])
            return True
        if text.startswith("mepdmverdict "):
            await self._send_review_verdict_dm(text[13:])
            return True
        if text.startswith("mepdmreplysafe "):
            await self._send_safe_dm_reply(text[15:])
            return True
        if text.startswith("mepdata "):
            parts = text.split(" ", 2)
            if len(parts) < 3:
                print(f"[{self.platform_name}] usage: mepdata <price> <payload>")
                return True
            await self._offer_data(parts[1], parts[2])
            return True
        if text.startswith("mepcancel "):
            parts = text.split(" ", 1)
            await self._cancel(parts[1])
            return True
        if text.startswith("mepresult "):
            parts = text.split(" ", 1)
            await self._result(parts[1])
            return True
        if text == "mepbalance":
            await self._balance()
            return True
        if text.startswith("mep "):
            await self._submit(text[4:])
            return True
        print(f"[{self.platform_name}] unknown command")
        return True

    async def run(self) -> None:
        await self.client.register()
        try:
            await self._announce_registry()
        except Exception as exc:
            print(f"[{self.platform_name}] registry announce warning: {exc}")
        listener = asyncio.create_task(self.client.listen_results(self._handle_result, self._handle_event))
        print(f"[{self.platform_name}] connected as {self.client.node_id}")
        print(
            f"[{self.platform_name}] commands: "
            "mep, mepdm, mepcall, mepcallaccept, mepcalldecline, mepcallframe, mepcallhangup, "
            "mepdmx, mepdmlist, mepdmsnapshot, mepdmhumanapproval, mepdmverdict, mepdmreplysafe, "
            "mepdata, mepcancel, mepresult, mepbalance, exit"
        )
        try:
            if self.noninteractive_keepalive:
                print(f"[{self.platform_name}] noninteractive keepalive enabled; stdin loop disabled")
                await listener
                return
            loop = asyncio.get_running_loop()
            keep_going = True
            while keep_going:
                line = await loop.run_in_executor(None, input, f"{self.platform_name}> ")
                keep_going = await self._dispatch_line(line)
        finally:
            self.client.stop()
            listener.cancel()


def run_stdio_adapter(platform_name: str, default_model: Optional[str] = None) -> None:
    model = default_model or f"{platform_name.lower()}-agent"
    key_file_name = f"mep_{platform_name.lower()}_adapter.pem"
    adapter = StdioAdapter(platform_name, model, key_file_name)
    asyncio.run(adapter.run())
