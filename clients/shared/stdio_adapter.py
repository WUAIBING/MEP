import asyncio
import os
import tempfile
from typing import Optional

from clients.shared.commands import parse_task_args
from clients.shared.mep_client import MEPClient

DEFAULT_BOUNTY = float(os.getenv("MEP_DEFAULT_BOUNTY", "5.0"))


class StdioAdapter:
    def __init__(self, platform_name: str, default_model: str, key_file_name: str):
        key_path = os.getenv("MEP_BOT_KEY_PATH", os.path.join(tempfile.gettempdir(), key_file_name))
        self.platform_name = platform_name
        self.default_model = default_model
        self.client = MEPClient(key_path)

    async def _handle_result(self, data: dict) -> None:
        task_id = data.get("task_id")
        result = data.get("result_payload", "")
        print(f"[{self.platform_name}] task_result {task_id}: {result}")

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
        print(f"[{self.platform_name}] commands: mep, mepdm, mepdata, mepcancel, mepresult, mepbalance, exit")
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
