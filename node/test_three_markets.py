import asyncio
import json
import os
import tempfile
import time
import urllib.parse
import uuid
from typing import Optional

import requests
import websockets

from identity import MEPIdentity
from task_envelope import build_task_envelope


HUB_URL = os.getenv("HUB_URL", "http://localhost:8000").rstrip("/")
WS_URL = os.getenv("WS_URL", "ws://localhost:8000").rstrip("/")


def get_auth_url(identity: MEPIdentity) -> str:
    ts = str(int(time.time()))
    sig_safe = urllib.parse.quote(identity.sign(identity.node_id, ts))
    return f"{WS_URL}/ws/{identity.node_id}?timestamp={ts}&signature={sig_safe}"


def _signed_post(identity: MEPIdentity, path: str, body: dict) -> dict:
    payload_str = json.dumps(body)
    headers = identity.get_auth_headers(payload_str)
    headers["Content-Type"] = "application/json"
    response = requests.post(f"{HUB_URL}{path}", data=payload_str, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def submit_task(
    identity: MEPIdentity,
    payload: str,
    bounty: float,
    *,
    target: Optional[str] = None,
    secret_data: Optional[str] = None,
) -> dict:
    body = build_task_envelope(
        identity.node_id,
        payload,
        bounty,
        target_node=target,
        secret_data=secret_data,
    )
    return _signed_post(identity, "/tasks/submit", body)


def place_bid(identity: MEPIdentity, task_id: str) -> dict:
    return _signed_post(identity, "/tasks/bid", {"task_id": task_id, "provider_id": identity.node_id})


def complete_task(identity: MEPIdentity, task_id: str, result: str) -> dict:
    return _signed_post(
        identity,
        "/tasks/complete",
        {
            "task_id": task_id,
            "provider_id": identity.node_id,
            "result_payload": result,
        },
    )


def get_balance(identity: MEPIdentity) -> float:
    response = requests.get(f"{HUB_URL}/balance/{identity.node_id}", timeout=10)
    response.raise_for_status()
    return float(response.json().get("balance_seconds", 0.0))


def register(identity: MEPIdentity) -> None:
    response = requests.post(f"{HUB_URL}/register", json={"pubkey": identity.pub_pem}, timeout=10)
    response.raise_for_status()


async def test_three_markets() -> None:
    print("=" * 60)
    print("MEP 3-market smoke test: compute, chat, data")
    print("=" * 60)

    key_dir = os.getenv("MEP_SMOKE_KEY_DIR", tempfile.gettempdir())
    alice = MEPIdentity(os.path.join(key_dir, f"alice_{uuid.uuid4().hex[:6]}.pem"))
    bob = MEPIdentity(os.path.join(key_dir, f"bob_{uuid.uuid4().hex[:6]}.pem"))

    register(alice)
    register(bob)

    print(f"Alice (sender/seller): {alice.node_id} | starting={get_balance(alice):.6f} SECONDS")
    print(f"Bob   (receiver/buyer): {bob.node_id} | starting={get_balance(bob):.6f} SECONDS")
    print()

    async def bob_listener() -> None:
        async with websockets.connect(get_auth_url(bob)) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            assert data["event"] == "rfc", data
            task = data["data"]
            assert task["bounty"] > 0, task
            task_id = task["id"]
            print(f"Bob received compute RFC {task_id[:8]} bounty={task['bounty']:.6f} SECONDS")
            bid_res = place_bid(bob, task_id)
            assert bid_res["status"] == "accepted", bid_res
            complete_task(bob, task_id, "market=compute\nresult=ok")

            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            assert data["event"] == "new_task", data
            task = data["data"]
            assert task["bounty"] == 0.0, task
            task_id = task["id"]
            print(f"Bob received chat task {task_id[:8]} payload={task['payload']!r}")
            complete_task(bob, task_id, "market=chat\nresult=received")

            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            assert data["event"] == "rfc", data
            task = data["data"]
            assert task["bounty"] < 0, task
            task_id = task["id"]
            cost = abs(float(task["bounty"]))
            print(f"Bob received data RFC {task_id[:8]} cost={cost:.6f} SECONDS")
            bid_res = place_bid(bob, task_id)
            assert bid_res["status"] == "accepted", bid_res
            assert bid_res["secret_data"] == "SECRET_TRADING_ALGO_V9", bid_res
            complete_task(bob, task_id, "market=data\nresult=received")

    async def alice_sender() -> None:
        await asyncio.sleep(0.5)
        async with websockets.connect(get_auth_url(alice)) as ws:
            print("Alice submits compute task: +5.0 SECONDS")
            submit_task(alice, "Write me a python script", 5.0)
            await asyncio.wait_for(ws.recv(), timeout=10)

            print("Alice sends targeted chat task: 0.0 SECONDS")
            chat_res = submit_task(alice, "Are you free to chat?", 0.0, target=bob.node_id)
            assert chat_res["status"] == "success", chat_res
            await asyncio.wait_for(ws.recv(), timeout=10)

            print("Alice offers data task: buyer pays 2.0 SECONDS")
            submit_task(
                alice,
                "Premium dataset available",
                -2.0,
                secret_data="SECRET_TRADING_ALGO_V9",
            )
            await asyncio.wait_for(ws.recv(), timeout=10)

    await asyncio.gather(bob_listener(), alice_sender())

    alice_balance = get_balance(alice)
    bob_balance = get_balance(bob)
    print()
    print("=" * 60)
    print("Final balances")
    print(f"Alice expected around 7.0 SECONDS, actual={alice_balance:.6f}")
    print(f"Bob   expected around 13.0 SECONDS, actual={bob_balance:.6f}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_three_markets())
