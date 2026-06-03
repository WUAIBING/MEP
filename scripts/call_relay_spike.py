#!/usr/bin/env python3
"""Local spike harness for the MEP call.* relay (hub/call_relay.py).

Validates the 6 agreed stop-condition scenarios plus the rejoin-timer vs
pong-timeout race, using an in-memory transport with injected latency jitter
(50-200ms scaled down) so ordering edges are exercised without a real socket.

Run: python scripts/call_relay_spike.py
Exit code 0 == all scenarios green.
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))
from call_relay import CallRelay  # noqa: E402

# scaled-down timings so the suite runs in ~seconds while preserving ratios
PING_INTERVAL = 0.05
TIMEOUT_MS = 200
GRACE_MS = 200
JITTER_RANGE = (0.005, 0.020)  # represents 50-200ms, scaled 10x for speed


class Mesh:
    """In-memory stand-in for connected_nodes + WS delivery, with jitter.

    Connected nodes auto-respond to call.ping with call.pong (as real clients
    do), unless auto-pong is disabled for that node to simulate a silent peer.
    """

    def __init__(self):
        self.connected = set()
        self.inbox = {}
        self.relay = None
        self.silent = set()  # nodes that do NOT auto-pong

    def connect(self, node_id):
        self.connected.add(node_id)
        self.inbox.setdefault(node_id, [])

    def disconnect(self, node_id):
        self.connected.discard(node_id)

    def is_online(self, node_id):
        return node_id in self.connected

    async def send(self, node_id, message):
        await asyncio.sleep(random.uniform(*JITTER_RANGE))
        if node_id not in self.connected:
            return False
        self.inbox.setdefault(node_id, []).append(message)
        if (
            message.get("event") == "call.ping"
            and self.relay is not None
            and node_id not in self.silent
        ):
            asyncio.ensure_future(
                self.relay.handle(node_id, {"event": "call.pong", "context_id": message["context_id"]})
            )
        return True

    def events(self, node_id, event=None):
        msgs = self.inbox.get(node_id, [])
        return [m for m in msgs if event is None or m.get("event") == event]

    def last(self, node_id, event=None):
        evs = self.events(node_id, event)
        return evs[-1] if evs else None


def _check(cond, label, fails):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        fails.append(label)


async def scenario_1_happy(fails):
    print("Scenario 1: happy path (invite->accept->ordered frames->hangup)")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c1", "callee": "B", "timeout_ms": TIMEOUT_MS})
    await asyncio.sleep(0.05)
    _check(mesh.last("B", "call.incoming") is not None, "B receives call.incoming", fails)
    await relay.handle("B", {"event": "call.accept", "context_id": "c1"})
    await asyncio.sleep(0.05)
    _check(mesh.last("A", "call.accepted") is not None, "A receives call.accepted", fails)
    for seq in range(5):
        await relay.handle("A", {"event": "call.frame", "context_id": "c1", "seq": seq, "payload": f"f{seq}"})
    await asyncio.sleep(0.2)
    frames = mesh.events("B", "call.frame")
    seqs = [f["seq"] for f in frames]
    _check(seqs == [0, 1, 2, 3, 4], f"B receives 5 frames in order (got {seqs})", fails)
    # idempotent dedup: replay seq 2
    await relay.handle("A", {"event": "call.frame", "context_id": "c1", "seq": 2, "payload": "dup"})
    await asyncio.sleep(0.1)
    _check(len(mesh.events("B", "call.frame")) == 5, "duplicate seq is dropped (still 5 frames)", fails)
    await relay.handle("A", {"event": "call.hangup", "context_id": "c1"})
    await asyncio.sleep(0.1)
    _check(mesh.last("B", "call.hangup") is not None, "B receives call.hangup", fails)
    _check(relay.get("c1") is None, "session removed after hangup (terminal completed)", fails)


async def scenario_2_decline(fails):
    print("Scenario 2: decline")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c2", "callee": "B", "timeout_ms": TIMEOUT_MS})
    await asyncio.sleep(0.05)
    await relay.handle("B", {"event": "call.decline", "context_id": "c2", "reason": "busy"})
    await asyncio.sleep(0.05)
    d = mesh.last("A", "call.declined")
    _check(d is not None and d.get("reason") == "busy", "A receives call.declined{busy}", fails)
    _check(relay.get("c2") is None, "session removed after decline", fails)


async def scenario_3_no_answer(fails):
    print("Scenario 3: no-answer timeout")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c3", "callee": "B", "timeout_ms": TIMEOUT_MS})
    await asyncio.sleep(TIMEOUT_MS / 1000.0 + 0.15)
    _check(mesh.last("A", "call.timeout") is not None, "A receives call.timeout (no accept)", fails)
    _check(relay.get("c3") is None, "session removed after timeout", fails)


async def scenario_4_offline_callee(fails):
    print("Scenario 4: offline callee -> fail-fast")
    mesh = Mesh()
    mesh.connect("A")  # B/C not connected
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c4", "callee": "C", "timeout_ms": TIMEOUT_MS})
    await asyncio.sleep(0.05)
    r = mesh.last("A", "call.rejected")
    _check(r is not None and r.get("reason") == "unavailable", "A receives call.rejected{unavailable}", fails)
    _check(relay.get("c4") is None, "no session created for offline callee", fails)


async def scenario_5_reconnect_within_grace(fails):
    print("Scenario 5: reconnect within grace -> call survives")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c5", "callee": "B", "timeout_ms": TIMEOUT_MS, "reconnect_grace_ms": GRACE_MS})
    await relay.handle("B", {"event": "call.accept", "context_id": "c5"})
    await asyncio.sleep(0.05)
    # B drops
    mesh.disconnect("B")
    await relay.on_node_disconnect("B")
    await asyncio.sleep(0.05)
    _check(mesh.last("A", "call.suspended") is not None, "A receives call.suspended on peer drop", fails)
    _check(relay.get("c5").state == "suspended", "session is suspended", fails)
    # B reconnects + resumes within grace
    await asyncio.sleep(GRACE_MS / 1000.0 * 0.3)
    mesh.connect("B")
    await relay.handle("B", {"event": "call.resume", "context_id": "c5"})
    await asyncio.sleep(0.05)
    _check(relay.get("c5") is not None and relay.get("c5").state == "active", "session active again after resume", fails)
    _check(mesh.last("A", "call.resumed") is not None, "A (peer) receives call.resumed", fails)
    _check(mesh.last("B", "call.resumed") is not None, "B (rejoiner) receives call.resumed", fails)
    # frames flow again
    await relay.handle("B", {"event": "call.frame", "context_id": "c5", "seq": 0, "payload": "after-resume"})
    await asyncio.sleep(0.1)
    _check(mesh.last("A", "call.frame") is not None, "frame flows after resume (no loss)", fails)
    await relay.handle("A", {"event": "call.hangup", "context_id": "c5"})
    await asyncio.sleep(0.1)
    _check(relay.get("c5") is None, "clean hangup after resume", fails)


async def scenario_6_grace_expiry(fails):
    print("Scenario 6: grace expiry -> peer_lost")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c6", "callee": "B", "timeout_ms": TIMEOUT_MS, "reconnect_grace_ms": GRACE_MS})
    await relay.handle("B", {"event": "call.accept", "context_id": "c6"})
    await asyncio.sleep(0.05)
    mesh.disconnect("B")
    await relay.on_node_disconnect("B")
    # do NOT resume; wait past grace
    await asyncio.sleep(GRACE_MS / 1000.0 + 0.2)
    hangups = mesh.events("A", "call.hangup")
    _check(len(hangups) == 1, f"A receives exactly ONE hangup (got {len(hangups)})", fails)
    _check(hangups and hangups[-1].get("terminal_state") == "peer_lost", "terminal_state == peer_lost", fails)
    _check(relay.get("c6") is None, "session removed after grace expiry", fails)


async def scenario_race(fails):
    print("Race check: rejoin within grace wins, no spurious pong-timeout teardown")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    await relay.handle("A", {"event": "call.invite", "context_id": "c7", "callee": "B", "timeout_ms": TIMEOUT_MS, "reconnect_grace_ms": GRACE_MS})
    await relay.handle("B", {"event": "call.accept", "context_id": "c7"})
    await asyncio.sleep(0.05)
    # B drops; suspend must cancel the ping loop so pong-timeout cannot fire
    mesh.disconnect("B")
    await relay.on_node_disconnect("B")
    _check(relay.get("c7").state == "suspended", "suspended immediately on drop (ping loop cancelled)", fails)
    # B reconnects + resumes WELL within grace (before grace timer fires)
    await asyncio.sleep(GRACE_MS / 1000.0 * 0.25)
    mesh.connect("B")
    await relay.handle("B", {"event": "call.resume", "context_id": "c7"})
    await asyncio.sleep(0.05)
    _check(relay.get("c7") is not None and relay.get("c7").state == "active", "rejoin wins: session active", fails)
    # run several ping cycles post-resume (auto-pong on) -> must stay alive, no hangup
    await asyncio.sleep(PING_INTERVAL * 5)
    _check(relay.get("c7") is not None and relay.get("c7").state == "active", "stays active across ping cycles after resume", fails)
    _check(mesh.events("A", "call.hangup") == [], "no spurious peer_lost hangup to A (single terminal owner)", fails)


async def scenario_8_pong_timeout(fails):
    print("Scenario 8: silent-but-connected peer -> pong-timeout peer_lost")
    mesh = Mesh()
    mesh.connect("A")
    mesh.connect("B")
    relay = CallRelay(mesh.send, is_online=mesh.is_online, ping_interval=PING_INTERVAL)
    mesh.relay = relay
    mesh.silent.add("B")  # B stays connected but never pongs
    await relay.handle("A", {"event": "call.invite", "context_id": "c8", "callee": "B", "timeout_ms": TIMEOUT_MS})
    await relay.handle("B", {"event": "call.accept", "context_id": "c8"})
    # A keeps ponging (auto); B is silent -> after 2 missed pongs B is declared lost
    await asyncio.sleep(PING_INTERVAL * 3)
    hangups = mesh.events("A", "call.hangup")
    _check(len(hangups) == 1 and hangups[-1].get("terminal_state") == "peer_lost",
           f"A (survivor) gets one peer_lost hangup for silent B (got {len(hangups)})", fails)
    _check(relay.get("c8") is None, "session torn down on pong-timeout", fails)


async def main():
    random.seed(7)
    fails = []
    for fn in (
        scenario_1_happy,
        scenario_2_decline,
        scenario_3_no_answer,
        scenario_4_offline_callee,
        scenario_5_reconnect_within_grace,
        scenario_6_grace_expiry,
        scenario_race,
        scenario_8_pong_timeout,
    ):
        await fn(fails)
    print("\n" + ("ALL GREEN" if not fails else f"FAILURES ({len(fails)}): " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
