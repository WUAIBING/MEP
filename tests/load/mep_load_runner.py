import argparse
import asyncio
import json
import os
import random
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import requests

from clients.shared.identity import MEPIdentity


@dataclass
class Phase:
    name: str
    duration_seconds: int
    submit_rps: float


class Metrics:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.submits_ok = 0
        self.submits_failed = 0
        self.bids_accepted = 0
        self.bids_rejected = 0
        self.completes_ok = 0
        self.completes_failed = 0
        self.ws_disconnects = 0
        self.errors = 0
        self.latencies: list[float] = []
        self._lock = asyncio.Lock()

    async def inc(self, field: str, amount: int = 1) -> None:
        async with self._lock:
            setattr(self, field, getattr(self, field) + amount)

    async def add_latency(self, value: float) -> None:
        async with self._lock:
            self.latencies.append(value)

    async def snapshot(self) -> dict:
        async with self._lock:
            lat = sorted(self.latencies)
            return {
                "uptime_sec": round(time.time() - self.started_at, 2),
                "submits_ok": self.submits_ok,
                "submits_failed": self.submits_failed,
                "bids_accepted": self.bids_accepted,
                "bids_rejected": self.bids_rejected,
                "completes_ok": self.completes_ok,
                "completes_failed": self.completes_failed,
                "ws_disconnects": self.ws_disconnects,
                "errors": self.errors,
                "results_count": len(lat),
                "p50_ms": _percentile_ms(lat, 0.50),
                "p95_ms": _percentile_ms(lat, 0.95),
                "p99_ms": _percentile_ms(lat, 0.99),
            }


def _percentile_ms(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    idx = max(0, min(len(values) - 1, int((len(values) - 1) * p)))
    return round(values[idx] * 1000.0, 2)


def build_task_request(node_id: str, payload: str, bounty: float) -> dict:
    if bounty < 0:
        market = "data"
        payment_direction = "receiver_to_sender"
    elif bounty == 0:
        market = "chat"
        payment_direction = "none"
    else:
        market = "compute"
        payment_direction = "sender_to_receiver"
    return {
        "source": {"node_id": node_id},
        "intent": {"type": "load.test.request"},
        "task": {
            "instructions": payload,
            "expected_output": {"result_type": "text"},
        },
        "economics": {
            "bounty_ns": int(abs(float(bounty)) * 1_000_000_000),
            "currency": "MEP_NS",
            "market": market,
            "payment_direction": payment_direction,
        },
    }


class VirtualNode:
    def __init__(
        self,
        node_index: int,
        key_dir: str,
        hub_url: str,
        ws_url: str,
        completion_delay_min: float,
        completion_delay_max: float,
        metrics: Metrics,
        runner: "LoadRunner",
    ) -> None:
        key_path = os.path.join(key_dir, f"node_{node_index:04d}.pem")
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.hub_url = hub_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.session = requests.Session()
        self.completion_delay_min = completion_delay_min
        self.completion_delay_max = completion_delay_max
        self.metrics = metrics
        self.runner = runner
        self.ws: Optional[Any] = None
        self.stop_event = asyncio.Event()

    def _auth_headers(self, payload_str: str) -> dict:
        headers = self.identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        return headers

    async def register(self) -> None:
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}/register",
            json={"pubkey": self.identity.pub_pem},
            timeout=15,
        )
        response.raise_for_status()

    async def _post_json(self, path: str, body: dict) -> tuple[int, dict]:
        payload_str = json.dumps(body)
        headers = self._auth_headers(payload_str)
        response = await asyncio.to_thread(
            self.session.post,
            f"{self.hub_url}{path}",
            data=payload_str,
            headers=headers,
            timeout=20,
        )
        try:
            data = response.json()
        except Exception:
            data = {"status": "error", "detail": response.text}
        return response.status_code, data

    async def submit_task(self, payload: str, bounty: float) -> Optional[str]:
        body = build_task_request(self.node_id, payload, bounty)
        code, data = await self._post_json("/tasks/submit", body)
        if code == 200 and data.get("status") == "success":
            await self.metrics.inc("submits_ok")
            task_id = data.get("task_id")
            if isinstance(task_id, str):
                self.runner.mark_submitted(task_id, time.time())
                return task_id
        await self.metrics.inc("submits_failed")
        return None

    async def _bid(self, task_id: str) -> tuple[bool, dict]:
        body = {"task_id": task_id, "provider_id": self.node_id}
        code, data = await self._post_json("/tasks/bid", body)
        accepted = code == 200 and data.get("status") == "accepted"
        if accepted:
            await self.metrics.inc("bids_accepted")
        else:
            await self.metrics.inc("bids_rejected")
        return accepted, data

    async def _complete(self, task_id: str, result_payload: str) -> bool:
        body = {"task_id": task_id, "provider_id": self.node_id, "result_payload": result_payload}
        code, data = await self._post_json("/tasks/complete", body)
        ok = code == 200 and data.get("status") == "success"
        if ok:
            await self.metrics.inc("completes_ok")
        else:
            await self.metrics.inc("completes_failed")
        return ok

    async def _handle_rfc(self, payload: dict) -> None:
        task_id = payload.get("id")
        if not isinstance(task_id, str):
            return
        accepted, bid_data = await self._bid(task_id)
        if not accepted:
            return
        delay = random.uniform(self.completion_delay_min, self.completion_delay_max)
        await asyncio.sleep(max(0.0, delay))
        task_payload = bid_data.get("payload", "")
        result = f"ok:{self.node_id}:{len(str(task_payload))}"
        await self._complete(task_id, result)

    async def connect_and_listen(self) -> None:
        from node.ws_connect import ws_connect

        while not self.stop_event.is_set():
            ts = str(int(time.time()))
            sig = urllib.parse.quote(self.identity.sign(self.node_id, ts))
            uri = f"{self.ws_url}/ws/{self.node_id}?timestamp={ts}&signature={sig}"
            try:
                self.ws = await ws_connect(uri, ping_interval=20, ping_timeout=20)
                while not self.stop_event.is_set():
                    raw = await self.ws.recv()
                    data = json.loads(raw)
                    event = data.get("event")
                    payload = data.get("data", {})
                    if event == "rfc":
                        await self._handle_rfc(payload)
                    elif event == "task_result":
                        task_id = payload.get("task_id")
                        if isinstance(task_id, str):
                            submitted_at = self.runner.pop_submitted(task_id)
                            if submitted_at is not None:
                                await self.metrics.add_latency(time.time() - submitted_at)
            except Exception:
                await self.metrics.inc("ws_disconnects")
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self.stop_event.set()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        await asyncio.to_thread(self.session.close)


class LoadRunner:
    def __init__(
        self,
        hub_url: str,
        ws_url: str,
        total_bots: int,
        consumers: int,
        bounty: float,
        completion_delay_min: float,
        completion_delay_max: float,
        phases: list[Phase],
        progress_interval: int,
        key_dir: Optional[str],
        register_rps: float,
    ) -> None:
        self.hub_url = hub_url
        self.ws_url = ws_url
        self.total_bots = total_bots
        self.consumers = min(max(1, consumers), total_bots)
        self.bounty = bounty
        self.completion_delay_min = completion_delay_min
        self.completion_delay_max = completion_delay_max
        self.phases = phases
        self.progress_interval = max(1, progress_interval)
        self.key_dir = key_dir or os.path.join(tempfile.gettempdir(), "mep_load_keys")
        self.register_rps = max(0.5, register_rps)
        self.metrics = Metrics()
        self.nodes: list[VirtualNode] = []
        self._submitted_tasks: dict[str, float] = {}

    def mark_submitted(self, task_id: str, submitted_at: float) -> None:
        self._submitted_tasks[task_id] = submitted_at

    def pop_submitted(self, task_id: str) -> Optional[float]:
        return self._submitted_tasks.pop(task_id, None)

    async def _build_nodes(self) -> None:
        os.makedirs(self.key_dir, exist_ok=True)
        self.nodes = [
            VirtualNode(
                node_index=i,
                key_dir=self.key_dir,
                hub_url=self.hub_url,
                ws_url=self.ws_url,
                completion_delay_min=self.completion_delay_min,
                completion_delay_max=self.completion_delay_max,
                metrics=self.metrics,
                runner=self,
            )
            for i in range(self.total_bots)
        ]
        interval = 1.0 / self.register_rps
        next_tick = time.time()
        for node in self.nodes:
            now = time.time()
            if now < next_tick:
                await asyncio.sleep(next_tick - now)
            await node.register()
            next_tick = max(next_tick + interval, time.time())

    async def _run_progress(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(self.progress_interval)
            snap = await self.metrics.snapshot()
            print(json.dumps({"progress": snap}, ensure_ascii=False))

    async def _run_phase_submissions(self, phase: Phase) -> None:
        consumer_nodes = self.nodes[: self.consumers]
        deadline = time.time() + phase.duration_seconds
        if phase.submit_rps <= 0:
            while time.time() < deadline:
                await asyncio.sleep(0.2)
            return
        interval = 1.0 / phase.submit_rps
        next_tick = time.time()
        i = 0
        while time.time() < deadline:
            now = time.time()
            if now < next_tick:
                await asyncio.sleep(next_tick - now)
                continue
            node = consumer_nodes[i % len(consumer_nodes)]
            i += 1
            payload = f"load-phase={phase.name};t={int(now)};n={i}"
            try:
                await node.submit_task(payload, self.bounty)
            except Exception:
                await self.metrics.inc("errors")
            next_tick += interval

    async def run(self) -> dict:
        await self._build_nodes()
        listeners = [asyncio.create_task(node.connect_and_listen()) for node in self.nodes]
        stop_progress = asyncio.Event()
        progress_task = asyncio.create_task(self._run_progress(stop_progress))
        try:
            for phase in self.phases:
                print(json.dumps({"phase_start": phase.name, "duration": phase.duration_seconds, "rps": phase.submit_rps}))
                await self._run_phase_submissions(phase)
                print(json.dumps({"phase_end": phase.name}))
            await asyncio.sleep(max(self.completion_delay_max * 2.0, 2.0))
        finally:
            stop_progress.set()
            progress_task.cancel()
            await asyncio.gather(*(node.stop() for node in self.nodes), return_exceptions=True)
            for task in listeners:
                task.cancel()
            await asyncio.gather(*listeners, return_exceptions=True)
        return await self.metrics.snapshot()


def _profile_config(name: str) -> tuple[int, int, list[Phase]]:
    normalized = name.strip().lower()
    if normalized == "stress":
        return 100, 20, [
            Phase("warmup", 120, 5.0),
            Phase("spike", 120, 12.0),
            Phase("recovery", 120, 5.0),
        ]
    return 100, 20, [Phase("baseline", 300, 6.0)]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["baseline", "stress"], default="baseline")
    parser.add_argument("--hub-url", default=os.getenv("HUB_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--ws-url", default=os.getenv("WS_URL", "ws://127.0.0.1:8000"))
    parser.add_argument("--bots", type=int, default=0)
    parser.add_argument("--consumers", type=int, default=0)
    parser.add_argument("--bounty", type=float, default=1.0)
    parser.add_argument("--completion-delay-min", type=float, default=0.3)
    parser.add_argument("--completion-delay-max", type=float, default=1.5)
    parser.add_argument("--progress-interval", type=int, default=15)
    parser.add_argument("--register-rps", type=float, default=4.0)
    parser.add_argument("--key-dir", default="")
    return parser.parse_args()


async def _run_from_args(args: argparse.Namespace) -> int:
    profile_bots, profile_consumers, phases = _profile_config(args.profile)
    bots = args.bots if args.bots > 0 else profile_bots
    consumers = args.consumers if args.consumers > 0 else profile_consumers
    key_dir = args.key_dir.strip() or None
    runner = LoadRunner(
        hub_url=args.hub_url,
        ws_url=args.ws_url,
        total_bots=bots,
        consumers=consumers,
        bounty=args.bounty,
        completion_delay_min=args.completion_delay_min,
        completion_delay_max=args.completion_delay_max,
        phases=phases,
        progress_interval=args.progress_interval,
        key_dir=key_dir,
        register_rps=args.register_rps,
    )
    print(json.dumps({"run_start": {"profile": args.profile, "bots": bots, "consumers": consumers}}))
    summary = await runner.run()
    print(json.dumps({"run_summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    namespace = _parse_args()
    raise SystemExit(asyncio.run(_run_from_args(namespace)))
