import asyncio
import os
import shlex
import tempfile
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
        self._recent_interbot_results: dict[str, dict[str, Any]] = {}

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

    def _remember_interbot_result(self, task_id: str, payload_text: str, message: dict[str, Any]) -> None:
        self._recent_interbot_results[task_id] = {
            "payload_text": payload_text,
            "message": message,
        }
        while len(self._recent_interbot_results) > 20:
            oldest_task_id = next(iter(self._recent_interbot_results))
            del self._recent_interbot_results[oldest_task_id]

    def _list_recent_structured_dm_results(self) -> None:
        if not self._recent_interbot_results:
            print(f"[{self.platform_name}] no stored structured dm results")
            return

        print(f"[{self.platform_name}] recent structured dm results:")
        for task_id, inbound in reversed(list(self._recent_interbot_results.items())):
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
        if len(parts) < 3:
            print(
                f"[{self.platform_name}] usage: mepdmreplysafe <task_id> <next_turn_index> <reply> "
                "[--checkpoint-summary text] [--turn-type type] [--intent type] [--priority <level>] [--human-note text]"
            )
            return

        task_id = parts[0]
        try:
            next_turn_index = int(parts[1])
        except ValueError:
            print(f"[{self.platform_name}] next_turn_index must be an integer")
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
        i = 2
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
            print(
                f"[{self.platform_name}] usage: mepdmreplysafe <task_id> <next_turn_index> <reply> [options]"
            )
            return

        stored = self._get_stored_structured_dm_context(task_id)
        if not stored:
            return
        message, _source, _target_node, _context_id, _reply_to_message_id = stored

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
        if len(parts) < 2:
            print(
                f"[{self.platform_name}] usage: mepdmhumanapproval <task_id> <summary> "
                "[--decision-type type] [--review-decision verdict] "
                "[--blocker text] [--next-action text] [--priority <level>] "
                "[--target-node node_id] [--target-alias alias] [--human-note text]"
            )
            return

        task_id = parts[0]
        summary_parts: list[str] = []
        decision_type = "merge_decision"
        review_decision: Optional[str] = None
        blockers: list[str] = []
        next_action: Optional[str] = None
        priority = "high"
        target_node_override: Optional[str] = None
        target_alias_override: Optional[str] = None
        human_note: Optional[str] = None
        i = 1
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
            print(
                f"[{self.platform_name}] usage: mepdmhumanapproval <task_id> <summary> [options]"
            )
            return

        stored = self._get_stored_structured_dm_context(task_id)
        if not stored:
            return
        _message, source, cached_target_node, context_id, reply_to_message_id = stored
        target_node = target_node_override or cached_target_node
        target_alias = target_alias_override
        if target_alias is None and not target_node_override and isinstance(source.get("alias"), str):
            target_alias = source.get("alias")

        try:
            response = await self.client.submit_human_approval_request_dm(
                summary,
                target_node,
                context_id=context_id,
                decision_type=decision_type,
                target_alias=target_alias,
                reply_to_task_id=task_id,
                reply_to_message_id=reply_to_message_id,
                review_decision=review_decision,
                blockers=blockers or None,
                recommended_next_action=next_action,
                priority=priority,
                human_note=human_note,
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
        if len(parts) < 3:
            print(
                f"[{self.platform_name}] usage: mepdmverdict <task_id> <verdict> <rationale> "
                "[--condition text] [--recommendation text] [--priority <level>] [--human-note text]"
            )
            return

        task_id = parts[0]
        verdict = parts[1]
        rationale_parts: list[str] = []
        conditions: list[str] = []
        recommendation: Optional[str] = None
        priority = "normal"
        human_note: Optional[str] = None
        i = 2
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
            print(
                f"[{self.platform_name}] usage: mepdmverdict <task_id> <verdict> <rationale> [options]"
            )
            return

        stored = self._get_stored_structured_dm_context(task_id)
        if not stored:
            return
        _message, source, target_node, context_id, reply_to_message_id = stored

        try:
            response = await self.client.submit_review_verdict_dm(
                verdict,
                rationale,
                target_node,
                context_id=context_id,
                target_alias=source.get("alias") if isinstance(source, dict) and isinstance(source.get("alias"), str) else None,
                reply_to_task_id=task_id,
                reply_to_message_id=reply_to_message_id if isinstance(reply_to_message_id, str) else None,
                conditions=conditions or None,
                human_recommendation=recommendation,
                priority=priority,
                human_note=human_note,
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
        if text.startswith("mepdmx "):
            await self._send_structured_dm(text[7:])
            return True
        if text == "mepdmlist":
            self._list_recent_structured_dm_results()
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
        listener = asyncio.create_task(self.client.listen_results(self._handle_result))
        print(f"[{self.platform_name}] connected as {self.client.node_id}")
        print(
            f"[{self.platform_name}] commands: "
            "mep, mepdm, mepdmx, mepdmlist, mepdmhumanapproval, mepdmverdict, mepdmreplysafe, "
            "mepdata, mepcancel, mepresult, mepbalance, exit"
        )
        loop = asyncio.get_running_loop()
        try:
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
