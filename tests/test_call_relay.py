"""Unit tests for the MEP call.* real-time relay (hub/call_relay.py).

Transport-agnostic: the relay is driven directly with an in-memory mesh that
records delivered events and auto-pongs on behalf of connected nodes (as real
clients do). Timings are scaled down so the suite runs in well under a second.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "hub"))

from call_relay import (  # noqa: E402
    CallRelay,
    _clamp_grace_ms,
    _clamp_timeout_ms,
    DEFAULT_GRACE_MS,
    MAX_GRACE_MS,
    DEFAULT_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
)

PING = 0.02
TIMEOUT_MS = 80
GRACE_MS = 80


class Mesh:
    def __init__(self):
        self.connected = set()
        self.inbox = {}
        self.relay = None
        self.silent = set()

    def connect(self, n):
        self.connected.add(n)
        self.inbox.setdefault(n, [])

    def disconnect(self, n):
        self.connected.discard(n)

    def is_online(self, n):
        return n in self.connected

    async def send(self, node_id, message):
        if node_id not in self.connected:
            return False
        self.inbox.setdefault(node_id, []).append(message)
        if message.get("event") == "call.ping" and self.relay and node_id not in self.silent:
            asyncio.ensure_future(
                self.relay.handle(node_id, {"event": "call.pong", "context_id": message["context_id"]})
            )
        return True

    def events(self, n, event=None):
        return [m for m in self.inbox.get(n, []) if event is None or m.get("event") == event]

    def last(self, n, event=None):
        evs = self.events(n, event)
        return evs[-1] if evs else None


def _mk(mesh):
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING)
    mesh.relay = relay
    return relay


async def _wait_until(predicate, timeout=2.0, step=PING / 2):
    """Await until predicate() is truthy (bounded), to avoid timing-fragile
    fixed sleeps in real-time relay tests."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


class CallRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_ordered_frames_and_dedup(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS})
        self.assertIsNotNone(mesh.last("B", "call.incoming"))
        await relay.handle("B", {"event": "call.accept", "context_id": "c"})
        self.assertIsNotNone(mesh.last("A", "call.accepted"))
        for seq in range(5):
            await relay.handle("A", {"event": "call.frame", "context_id": "c", "seq": seq, "payload": f"f{seq}"})
        await asyncio.sleep(0.02)
        self.assertEqual([f["seq"] for f in mesh.events("B", "call.frame")], [0, 1, 2, 3, 4])
        # duplicate seq is ignored
        await relay.handle("A", {"event": "call.frame", "context_id": "c", "seq": 2, "payload": "dup"})
        self.assertEqual(len(mesh.events("B", "call.frame")), 5)
        await relay.handle("A", {"event": "call.hangup", "context_id": "c"})
        self.assertIsNotNone(mesh.last("B", "call.hangup"))
        self.assertIsNone(relay.get("c"))

    async def test_decline(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS})
        await relay.handle("B", {"event": "call.decline", "context_id": "c", "reason": "busy"})
        d = mesh.last("A", "call.declined")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("reason"), "busy")
        self.assertIsNone(relay.get("c"))

    async def test_no_answer_timeout(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS})
        await asyncio.sleep(TIMEOUT_MS / 1000.0 + 0.05)
        self.assertIsNotNone(mesh.last("A", "call.timeout"))
        self.assertIsNone(relay.get("c"))

    async def test_offline_callee_fail_fast(self):
        mesh = Mesh()
        mesh.connect("A")
        relay = _mk(mesh)  # callee not connected
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "C", "timeout_ms": TIMEOUT_MS})
        r = mesh.last("A", "call.rejected")
        self.assertIsNotNone(r)
        self.assertEqual(r.get("reason"), "unavailable")
        self.assertIsNone(relay.get("c"))

    async def test_reconnect_within_grace_survives(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS, "reconnect_grace_ms": GRACE_MS})
        await relay.handle("B", {"event": "call.accept", "context_id": "c"})
        mesh.disconnect("B")
        await relay.on_node_disconnect("B")
        self.assertEqual(relay.get("c").state, "suspended")
        self.assertIsNotNone(mesh.last("A", "call.suspended"))
        await asyncio.sleep(GRACE_MS / 1000.0 * 0.25)
        mesh.connect("B")
        await relay.handle("B", {"event": "call.resume", "context_id": "c"})
        self.assertEqual(relay.get("c").state, "active")
        self.assertIsNotNone(mesh.last("A", "call.resumed"))
        self.assertIsNotNone(mesh.last("B", "call.resumed"))
        await relay.handle("B", {"event": "call.frame", "context_id": "c", "seq": 0, "payload": "x"})
        await asyncio.sleep(0.01)
        self.assertIsNotNone(mesh.last("A", "call.frame"))

    async def test_grace_expiry_peer_lost(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS, "reconnect_grace_ms": GRACE_MS})
        await relay.handle("B", {"event": "call.accept", "context_id": "c"})
        mesh.disconnect("B")
        await relay.on_node_disconnect("B")
        await asyncio.sleep(GRACE_MS / 1000.0 + 0.06)
        hangups = mesh.events("A", "call.hangup")
        self.assertEqual(len(hangups), 1)
        self.assertEqual(hangups[-1].get("terminal_state"), "peer_lost")
        self.assertIsNone(relay.get("c"))

    async def test_rejoin_wins_no_spurious_teardown(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS, "reconnect_grace_ms": GRACE_MS})
        await relay.handle("B", {"event": "call.accept", "context_id": "c"})
        mesh.disconnect("B")
        await relay.on_node_disconnect("B")
        self.assertEqual(relay.get("c").state, "suspended")
        await asyncio.sleep(GRACE_MS / 1000.0 * 0.25)
        mesh.connect("B")
        await relay.handle("B", {"event": "call.resume", "context_id": "c"})
        await asyncio.sleep(PING * 5)
        self.assertIsNotNone(relay.get("c"))
        self.assertEqual(relay.get("c").state, "active")
        self.assertEqual(mesh.events("A", "call.hangup"), [])

    async def test_silent_peer_pong_timeout(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        mesh.silent.add("B")
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": TIMEOUT_MS})
        await relay.handle("B", {"event": "call.accept", "context_id": "c"})
        # Poll for the silent-peer teardown instead of racing a fixed sleep, so
        # the test isn't timing-fragile under a slow/loaded runner. Teardown
        # removes the session and emits exactly one hangup, so we can wait for
        # the first hangup and then assert the terminal outcome.
        await _wait_until(lambda: mesh.events("A", "call.hangup"))
        hangups = mesh.events("A", "call.hangup")
        self.assertEqual(len(hangups), 1)
        self.assertEqual(hangups[-1].get("terminal_state"), "peer_lost")
        self.assertIsNone(relay.get("c"))

    async def test_cap_rejects_excess_calls(self):
        mesh = Mesh()
        mesh.connect("A")
        for n in ("B", "C", "D", "E"):
            mesh.connect(n)
        relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING, max_calls_per_node=2)
        mesh.relay = relay
        await relay.handle("A", {"event": "call.invite", "context_id": "c1", "callee": "B", "timeout_ms": TIMEOUT_MS})
        await relay.handle("A", {"event": "call.invite", "context_id": "c2", "callee": "C", "timeout_ms": TIMEOUT_MS})
        await relay.handle("A", {"event": "call.invite", "context_id": "c3", "callee": "D", "timeout_ms": TIMEOUT_MS})
        rejected = [m for m in mesh.events("A", "call.rejected") if m.get("reason") == "cap"]
        self.assertEqual(len(rejected), 1)

    def test_clamp_grace(self):
        self.assertEqual(_clamp_grace_ms(None), DEFAULT_GRACE_MS)
        self.assertEqual(_clamp_grace_ms(-5), 0)
        self.assertEqual(_clamp_grace_ms(999_999), MAX_GRACE_MS)
        self.assertEqual(_clamp_grace_ms(12_345), 12_345)
        self.assertEqual(_clamp_grace_ms("bad"), DEFAULT_GRACE_MS)

    def test_clamp_timeout(self):
        # missing/invalid -> default (never raises on untrusted WS input)
        self.assertEqual(_clamp_timeout_ms(None), DEFAULT_TIMEOUT_MS)
        self.assertEqual(_clamp_timeout_ms("bad"), DEFAULT_TIMEOUT_MS)
        self.assertEqual(_clamp_timeout_ms([1, 2]), DEFAULT_TIMEOUT_MS)
        # zero/negative -> default (a 0/negative timeout would fire instantly)
        self.assertEqual(_clamp_timeout_ms(0), DEFAULT_TIMEOUT_MS)
        self.assertEqual(_clamp_timeout_ms(-100), DEFAULT_TIMEOUT_MS)
        # absurdly large capped to MAX (can't pin an invite session/timer)
        self.assertEqual(_clamp_timeout_ms(10**9), MAX_TIMEOUT_MS)
        # legitimate small + in-range positive values pass through unchanged
        self.assertEqual(_clamp_timeout_ms(80), 80)
        self.assertEqual(_clamp_timeout_ms(45_000), 45_000)

    async def test_invite_with_invalid_timeout_does_not_raise(self):
        mesh = Mesh()
        mesh.connect("A")
        mesh.connect("B")
        relay = _mk(mesh)
        # a non-numeric timeout_ms must not raise inside the handler; the
        # session is created with the default timeout instead.
        await relay.handle("A", {"event": "call.invite", "context_id": "c", "callee": "B", "timeout_ms": "garbage"})
        self.assertIsNotNone(mesh.last("B", "call.incoming"))
        self.assertEqual(relay.get("c").timeout_ms, DEFAULT_TIMEOUT_MS)


if __name__ == "__main__":
    unittest.main()
