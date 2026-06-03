#!/usr/bin/env python3
"""End-to-end call.* test over REAL WebSockets against a running hub.

Unlike scripts/call_relay_spike.py (in-memory transport), this drives the actual
hub: two nodes register via REST, open authenticated /ws/{node_id} sockets, and
exchange real call.* frames. Proves the wiring in hub/main.py works on live
sockets, including a mid-call WS drop -> resume within grace.

Usage:
    MEP_HUB=http://127.0.0.1:8077 python scripts/call_relay_e2e.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.parse

import requests
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "node"))
from identity import MEPIdentity  # noqa: E402

HUB = os.getenv("MEP_HUB", "http://127.0.0.1:8077")
WS = HUB.replace("http://", "ws://").replace("https://", "wss://")
HOST = "localhost"
fails = []


def _check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


class Node:
    def __init__(self, name):
        d = tempfile.mkdtemp(prefix=f"mep_{name}_")
        self.identity = MEPIdentity(os.path.join(d, "k.pem"))
        self.node_id = self.identity.node_id
        self.ws = None
        self.rx = []
        self._task = None

    def register(self):
        r = requests.post(f"{HUB}/register", json={"pubkey": self.identity.pub_pem, "alias": self.node_id},
                          headers={"Host": HOST}, timeout=10)
        r.raise_for_status()

    async def connect(self):
        ts = str(int(time.time()))
        sig = urllib.parse.quote(self.identity.sign(self.node_id, ts))
        url = f"{WS}/ws/{self.node_id}?timestamp={ts}&signature={sig}"
        self.ws = await websockets.connect(url)
        self.rx = []
        self._task = asyncio.ensure_future(self._reader())

    async def _reader(self):
        try:
            async for raw in self.ws:
                try:
                    self.rx.append(json.loads(raw))
                except ValueError:
                    pass
        except Exception:
            pass

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def drop(self):
        if self._task:
            self._task.cancel()
        await self.ws.close()

    def events(self, event=None):
        return [m for m in self.rx if event is None or m.get("event") == event]

    def last(self, event=None):
        evs = self.events(event)
        return evs[-1] if evs else None


async def wait_for(node, event, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if node.events(event):
            return True
        await asyncio.sleep(0.05)
    return False


async def main():
    a, b = Node("A"), Node("B")
    a.register()
    b.register()
    await a.connect()
    await b.connect()
    await asyncio.sleep(0.3)
    ctx = f"e2e-{int(time.time())}"

    print("E2E: invite -> accept -> frames -> hangup (real sockets)")
    await a.send({"event": "call.invite", "context_id": ctx, "callee": b.node_id,
                  "timeout_ms": 5000, "reconnect_grace_ms": 3000})
    _check(await wait_for(b, "call.incoming"), "B receives call.incoming")
    await b.send({"event": "call.accept", "context_id": ctx})
    _check(await wait_for(a, "call.accepted"), "A receives call.accepted")
    for seq in range(4):
        await a.send({"event": "call.frame", "context_id": ctx, "seq": seq, "payload": f"hi-{seq}"})
    await asyncio.sleep(0.4)
    got = [f.get("seq") for f in b.events("call.frame")]
    _check(got == [0, 1, 2, 3], f"B receives 4 frames in order (got {got})")
    # B answers back over the same call
    await b.send({"event": "call.frame", "context_id": ctx, "seq": 0, "payload": "ack"})
    _check(await wait_for(a, "call.frame"), "A receives B's reply frame (bidirectional)")

    print("E2E: mid-call WS drop -> resume within grace -> frames continue")
    await b.drop()
    _check(await wait_for(a, "call.suspended"), "A receives call.suspended on B drop")
    await asyncio.sleep(0.3)
    await b.connect()  # B reconnects
    await b.send({"event": "call.resume", "context_id": ctx})
    _check(await wait_for(a, "call.resumed"), "A receives call.resumed")
    _check(await wait_for(b, "call.resumed"), "B receives call.resumed")
    await a.send({"event": "call.frame", "context_id": ctx, "seq": 99, "payload": "after-resume"})
    _check(await wait_for(b, "call.frame"), "B receives frame after resume (no call loss)")

    print("E2E: hangup -> terminal")
    await a.send({"event": "call.hangup", "context_id": ctx})
    _check(await wait_for(b, "call.hangup"), "B receives call.hangup")

    try:
        await a.drop()
        await b.drop()
    except Exception:
        pass
    print("\n" + ("ALL GREEN" if not fails else f"FAILURES ({len(fails)}): " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
