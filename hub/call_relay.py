"""MEP call.* real-time DM relay (v1 prototype).

Implements the live "phone-call" lane agreed in design: two already-connected
nodes establish a call session over their existing authenticated WebSockets, and
the hub relays frames peer-to-peer in-memory (no task rows on the hot path).

Transport-agnostic: the relay is given an async ``send_fn(node_id, message)``
that delivers a JSON-able dict to a node's current socket (returns True on
success). The hub wires this to ``connected_nodes[node_id].send_json``; tests
wire it to in-memory fakes. Time/timer behaviour is injectable so the state
machine can be validated deterministically with small intervals.

Session lifecycle / states:
    inviting  -> active        (callee accepts)
    inviting  -> terminal      (decline | timeout | rejected)
    active    -> suspended     (a participant's WS drops)
    suspended -> active        (call.resume within grace)
    suspended -> terminal      (grace expires -> peer_lost)
    active    -> terminal      (hangup | 2 missed pongs -> peer_lost)

Terminal states: completed | declined | timeout | peer_lost | rejected.

Determinism note (rejoin-timer vs pong-timeout race): liveness pings run ONLY
while a session is ``active``. The instant a participant disconnects the session
moves to ``suspended`` and the ping loop is cancelled, so the only armed teardown
is the grace timer. Conversely a silent-but-connected peer is handled only by the
pong timeout. Exactly one teardown mechanism owns a session per state, so the two
timers can never race to two different terminal states.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Tuple

SendFn = Callable[[str, dict], Awaitable[bool]]

TERMINAL_STATES = {"completed", "declined", "timeout", "peer_lost", "rejected"}

DEFAULT_GRACE_MS = 10_000
MAX_GRACE_MS = 60_000
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 300_000
MAX_CALLS_PER_NODE = 3
MISSED_PONG_LIMIT = 2


def _clamp_grace_ms(value: Optional[float]) -> int:
    if value is None:
        return DEFAULT_GRACE_MS
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GRACE_MS
    return max(0, min(MAX_GRACE_MS, v))


def _clamp_timeout_ms(value: Optional[float]) -> int:
    """Validate/clamp a caller-supplied invite timeout. Untrusted WS input:
    default on missing/invalid/non-positive (a 0/negative timeout would fire
    instantly), and cap to MAX so a caller can't pin an invite session/timer."""
    if value is None:
        return DEFAULT_TIMEOUT_MS
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    if v <= 0:
        return DEFAULT_TIMEOUT_MS
    return min(MAX_TIMEOUT_MS, v)


@dataclass
class CallSession:
    context_id: str
    caller: str
    callee: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    grace_ms: int = DEFAULT_GRACE_MS
    state: str = "inviting"
    created_at: float = field(default_factory=time.time)
    # last seq seen per sender (idempotent dedup of relayed frames)
    seq_seen: Dict[str, int] = field(default_factory=dict)
    missed_pong: Dict[str, int] = field(default_factory=dict)
    # internal asyncio handles
    _invite_timer: Optional[asyncio.Task] = None
    _ping_task: Optional[asyncio.Task] = None
    _grace_timer: Optional[asyncio.Task] = None
    suspended_node: Optional[str] = None

    def peer_of(self, node_id: str) -> Optional[str]:
        if node_id == self.caller:
            return self.callee
        if node_id == self.callee:
            return self.caller
        return None

    def participants(self) -> Tuple[str, str]:
        return (self.caller, self.callee)


class CallRelay:
    def __init__(
        self,
        send_fn: SendFn,
        *,
        is_online: Callable[[str], bool],
        ping_interval: float = 5.0,
        missed_pong_limit: int = MISSED_PONG_LIMIT,
        max_calls_per_node: int = MAX_CALLS_PER_NODE,
    ) -> None:
        self._send = send_fn
        self._is_online = is_online
        self._ping_interval = ping_interval
        self._missed_pong_limit = missed_pong_limit
        self._max_calls_per_node = max_calls_per_node
        self._sessions: Dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    # ---- introspection helpers (used by tests) ----
    def get(self, context_id: str) -> Optional[CallSession]:
        return self._sessions.get(context_id)

    def active_count_for(self, node_id: str) -> int:
        return sum(
            1
            for s in self._sessions.values()
            if node_id in (s.caller, s.callee) and s.state not in TERMINAL_STATES
        )

    # ---- inbound event dispatch ----
    async def handle(self, node_id: str, message: dict) -> None:
        event = message.get("event", "")
        handler = {
            "call.invite": self._on_invite,
            "call.accept": self._on_accept,
            "call.decline": self._on_decline,
            "call.frame": self._on_frame,
            "call.hangup": self._on_hangup,
            "call.pong": self._on_pong,
            "call.resume": self._on_resume,
        }.get(event)
        if handler is None:
            return
        await handler(node_id, message)

    async def _on_invite(self, caller: str, msg: dict) -> None:
        context_id = msg.get("context_id")
        callee = msg.get("callee")
        if not context_id or not callee:
            await self._send(caller, {"event": "call.rejected", "context_id": context_id, "reason": "bad_request"})
            return
        async with self._lock:
            if context_id in self._sessions:
                await self._send(caller, {"event": "call.rejected", "context_id": context_id, "reason": "duplicate_context"})
                return
            # replay / staleness guard (untrusted input -> reject unparseable)
            expires_at = msg.get("expires_at")
            if expires_at is not None:
                try:
                    expired = float(expires_at) < time.time()
                except (TypeError, ValueError):
                    await self._send(caller, {"event": "call.rejected", "context_id": context_id, "reason": "bad_request"})
                    return
                if expired:
                    await self._send(caller, {"event": "call.rejected", "context_id": context_id, "reason": "expired_invite"})
                    return
            if self.active_count_for(caller) >= self._max_calls_per_node:
                await self._send(caller, {"event": "call.rejected", "context_id": context_id, "reason": "cap"})
                return
            if not self._is_online(callee):
                await self._send(caller, {"event": "call.rejected", "context_id": context_id, "reason": "unavailable"})
                return
            session = CallSession(
                context_id=context_id,
                caller=caller,
                callee=callee,
                timeout_ms=_clamp_timeout_ms(msg.get("timeout_ms")),
                grace_ms=_clamp_grace_ms(msg.get("reconnect_grace_ms")),
            )
            self._sessions[context_id] = session
            session._invite_timer = asyncio.ensure_future(self._invite_timeout(context_id))
        # ring the callee
        await self._send(callee, {"event": "call.incoming", "context_id": context_id, "caller": caller, "timeout_ms": session.timeout_ms})

    async def _invite_timeout(self, context_id: str) -> None:
        session = self._sessions.get(context_id)
        if not session:
            return
        try:
            await asyncio.sleep(session.timeout_ms / 1000.0)
        except asyncio.CancelledError:
            return
        async with self._lock:
            s = self._sessions.get(context_id)
            if not s or s.state != "inviting":
                return
            self._terminate(s, "timeout")
        await self._send(session.caller, {"event": "call.timeout", "context_id": context_id})
        await self._send(session.callee, {"event": "call.cancelled", "context_id": context_id})

    async def _on_accept(self, node_id: str, msg: dict) -> None:
        context_id = msg.get("context_id")
        async with self._lock:
            s = self._sessions.get(context_id)
            if not s or s.state != "inviting" or node_id != s.callee:
                return
            self._cancel(s._invite_timer)
            s._invite_timer = None
            s.state = "active"
            s.missed_pong = {s.caller: 0, s.callee: 0}
            s._ping_task = asyncio.ensure_future(self._ping_loop(context_id))
        await self._send(s.caller, {"event": "call.accepted", "context_id": context_id})

    async def _on_decline(self, node_id: str, msg: dict) -> None:
        context_id = msg.get("context_id")
        async with self._lock:
            s = self._sessions.get(context_id)
            if not s or s.state != "inviting" or node_id != s.callee:
                return
            self._terminate(s, "declined")
        await self._send(s.caller, {"event": "call.declined", "context_id": context_id, "reason": msg.get("reason")})

    async def _on_frame(self, node_id: str, msg: dict) -> None:
        # v1 design choice: protocol violations (frame for an unknown/non-active
        # session, or from a WS identity that isn't a participant) are dropped
        # silently rather than answered with an error. They're caller bugs, not
        # runtime conditions; revisit for v2 if we want explicit call.error codes.
        context_id = msg.get("context_id")
        s = self._sessions.get(context_id)
        if not s or s.state != "active":
            return
        peer = s.peer_of(node_id)
        if peer is None:
            return  # WS identity not a participant -> drop
        seq = msg.get("seq")
        if isinstance(seq, int):
            if seq <= s.seq_seen.get(node_id, -1):
                return  # idempotent dedup
            s.seq_seen[node_id] = seq
        await self._send(
            peer,
            {
                "event": "call.frame",
                "context_id": context_id,
                "seq": seq,
                "ts": msg.get("ts") or time.time(),
                "sender": node_id,
                "content_type": msg.get("content_type", "text/plain"),
                "payload": msg.get("payload"),
            },
        )

    async def _on_hangup(self, node_id: str, msg: dict) -> None:
        context_id = msg.get("context_id")
        async with self._lock:
            s = self._sessions.get(context_id)
            if not s or s.state in TERMINAL_STATES:
                return
            peer = s.peer_of(node_id)
            if peer is None:
                return
            self._terminate(s, "completed")
        await self._send(peer, {"event": "call.hangup", "context_id": context_id, "reason": "remote_hangup", "terminal_state": "completed"})

    async def _on_pong(self, node_id: str, msg: dict) -> None:
        context_id = msg.get("context_id")
        s = self._sessions.get(context_id)
        if not s or s.state != "active":
            return
        if node_id in s.missed_pong:
            s.missed_pong[node_id] = 0

    async def _ping_loop(self, context_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                # Decide state transitions under the lock, but defer all socket
                # sends until after releasing it so a stalled send can't block
                # other relay state transitions.
                ping_targets: Tuple[str, ...] = ()
                async with self._lock:
                    s = self._sessions.get(context_id)
                    if not s or s.state != "active":
                        return
                    dead = None
                    for p in s.participants():
                        s.missed_pong[p] = s.missed_pong.get(p, 0) + 1
                        if s.missed_pong[p] >= self._missed_pong_limit:
                            dead = p
                    if dead is not None:
                        survivor = s.peer_of(dead)
                        self._terminate(s, "peer_lost")
                        target = survivor
                    else:
                        target = None
                        ping_targets = s.participants()
                for p in ping_targets:
                    await self._send(p, {"event": "call.ping", "context_id": context_id})
                if target is not None:
                    await self._send(target, {"event": "call.hangup", "context_id": context_id, "reason": "peer_lost", "terminal_state": "peer_lost"})
                    return
        except asyncio.CancelledError:
            return

    async def on_node_disconnect(self, node_id: str) -> None:
        """Called by the hub when a node's WS drops. Suspends affected active
        sessions and arms the reconnect grace timer (single teardown owner)."""
        to_suspend = []
        async with self._lock:
            for s in self._sessions.values():
                if s.state == "active" and node_id in (s.caller, s.callee):
                    s.state = "suspended"
                    s.suspended_node = node_id
                    self._cancel(s._ping_task)
                    s._ping_task = None
                    s._grace_timer = asyncio.ensure_future(self._grace_timeout(s.context_id))
                    to_suspend.append((s.context_id, s.peer_of(node_id)))
        for context_id, peer in to_suspend:
            if peer is not None:
                await self._send(peer, {"event": "call.suspended", "context_id": context_id, "reason": "peer_disconnected"})

    async def _grace_timeout(self, context_id: str) -> None:
        s = self._sessions.get(context_id)
        if not s:
            return
        try:
            await asyncio.sleep(s.grace_ms / 1000.0)
        except asyncio.CancelledError:
            return
        async with self._lock:
            s2 = self._sessions.get(context_id)
            if not s2 or s2.state != "suspended":
                return
            survivor = s2.peer_of(s2.suspended_node) if s2.suspended_node else None
            self._terminate(s2, "peer_lost")
        if survivor is not None:
            await self._send(survivor, {"event": "call.hangup", "context_id": context_id, "reason": "peer_lost", "terminal_state": "peer_lost"})

    async def _on_resume(self, node_id: str, msg: dict) -> None:
        context_id = msg.get("context_id")
        async with self._lock:
            s = self._sessions.get(context_id)
            if not s or s.state != "suspended" or node_id != s.suspended_node:
                return
            self._cancel(s._grace_timer)
            s._grace_timer = None
            s.suspended_node = None
            s.state = "active"
            s.missed_pong = {s.caller: 0, s.callee: 0}
            s._ping_task = asyncio.ensure_future(self._ping_loop(context_id))
            peer = s.peer_of(node_id)
        await self._send(node_id, {"event": "call.resumed", "context_id": context_id, "role": "rejoiner"})
        if peer is not None:
            await self._send(peer, {"event": "call.resumed", "context_id": context_id, "role": "peer"})

    # ---- internals ----
    def _terminate(self, session: CallSession, terminal_state: str) -> None:
        session.state = terminal_state
        # Never cancel the task we are currently running inside: a timer
        # coroutine (invite/grace/ping) calls _terminate, and cancelling its
        # own task here would abort the notification send that follows.
        current = asyncio.current_task()
        for task in (session._invite_timer, session._ping_task, session._grace_timer):
            self._cancel(task, current)
        session._invite_timer = session._ping_task = session._grace_timer = None
        self._sessions.pop(session.context_id, None)

    @staticmethod
    def _cancel(task: Optional[asyncio.Task], current: Optional[asyncio.Task] = None) -> None:
        if task is not None and task is not current and not task.done():
            task.cancel()
