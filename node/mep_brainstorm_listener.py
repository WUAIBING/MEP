#!/usr/bin/env python3
"""
mep_brainstorm_listener.py — Reference MEP listener with brainstorm session support.
Drop-in template using the live Hub API (PR #103).

Hub endpoints used:
  POST /brainstorm/sessions/create   — Create a session
  POST /brainstorm/sessions/post     — Post a message (Hub fans out to all)
  GET  /brainstorm/sessions          — List my sessions
  GET  /brainstorm/sessions/{id}     — Get session details

WebSocket events received:
  brainstorm_message — Fanout from Hub when any participant posts

Usage:
  pip install websockets requests
  python3 mep_brainstorm_listener.py
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.parse

import requests
import websockets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from node.identity import MEPIdentity
except ImportError:
    sys.path.insert(0, os.path.expanduser("~/.hermes"))
    from node.identity import MEPIdentity

# ── Config ──────────────────────────────────────────────────────────────────
HUB = os.environ.get('MEP_HUB_URL', 'https://mep-hub.silentcopilot.ai')
WS = os.environ.get('MEP_HUB_WS', 'wss://mep-hub.silentcopilot.ai')
KEY_PATH = os.environ.get('MEP_KEY_PATH', os.path.expanduser('~/.hermes/moltbot_mep_node.pem'))

identity = MEPIdentity(key_path=KEY_PATH)
NODE_ID = identity.node_id
NODE_ALIAS = os.environ.get('MEP_NODE_ALIAS', 'BrainstormBot')

MINIMAX_API_KEY = os.environ.get('MINIMAX_API_KEY', '')
MINIMAX_BASE_URL = os.environ.get('MINIMAX_BASE_URL', 'https://api.minimax.chat/v1')

MAX_EXCHANGES_PER_SENDER = 50
conversation_counts: dict[str, int] = {}
known_sessions: dict[str, dict] = {}  # session_id → {topic, participants, ...}


# ── Auth Helper ─────────────────────────────────────────────────────────────
def auth_headers(body: str) -> dict:
    return {'Content-Type': 'application/json', **identity.get_auth_headers(body)}


# ── AI Reply ────────────────────────────────────────────────────────────────
def ai_reply(context: str) -> str:
    """Generate AI reply via MiniMax API."""
    if not MINIMAX_API_KEY:
        return f"[{NODE_ALIAS}: no API key configured]"
    try:
        payload = json.dumps({
            "model": "MiniMax-M2.5",
            "messages": [
                {"role": "system", "content": f"You are {NODE_ALIAS}, MEP agent. Be concise, technical, direct. 2-4 sentences. Never give generic acks — always contribute substantive analysis."},
                {"role": "user", "content": context},
            ],
            "max_tokens": 500, "temperature": 0.7,
        })
        r = requests.post(
            f"{MINIMAX_BASE_URL}/text/chatcompletion_v2",
            headers={"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"},
            data=payload, timeout=30
        )
        if r.status_code == 200:
            raw = r.json()["choices"][0]["message"]["content"]
            return re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
        print(f"[{NODE_ALIAS}] MiniMax {r.status_code}: {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"[{NODE_ALIAS}] MiniMax error: {e}", flush=True)
    return f"[{NODE_ALIAS}: AI error]"


# ── Session Helpers ─────────────────────────────────────────────────────────
def create_session(participants: list[str], topic: str = "", max_msgs: int = 100) -> str | None:
    """Create a new brainstorm session. Returns session_id or None."""
    if NODE_ID not in participants:
        participants = list(participants) + [NODE_ID]
    body = json.dumps({
        'owner_id': NODE_ID, 'participants': participants,
        'topic': topic.strip(), 'max_messages': max_msgs
    })
    r = requests.post(f'{HUB}/brainstorm/sessions/create', headers=auth_headers(body), data=body, timeout=10)
    if r.status_code == 200:
        data = r.json()
        sid = data['session_id']
        known_sessions[sid] = {'topic': topic, 'participants': participants}
        print(f'[{NODE_ALIAS}] Created session {sid[:8]}: {topic}', flush=True)
        return sid
    print(f'[{NODE_ALIAS}] Create failed: {r.status_code} {r.text[:200]}', flush=True)
    return None


def post_to_session(session_id: str, message: str, reply_to: str = None) -> bool:
    """Post a message to a brainstorm session. Hub fans out to all participants."""
    body = json.dumps({
        'session_id': session_id,
        'message': message,
        'reply_to_message_id': reply_to
    })
    r = requests.post(f'{HUB}/brainstorm/sessions/post', headers=auth_headers(body), data=body, timeout=10)
    if r.status_code == 200:
        data = r.json()
        print(f'[{NODE_ALIAS}] Posted → {session_id[:8]} (delivered to {len(data.get("delivered_to", []))})', flush=True)
        return True
    print(f'[{NODE_ALIAS}] Post failed: {r.status_code} {r.text[:200]}', flush=True)
    return False


def get_session(session_id: str) -> dict | None:
    """Fetch session details including recent messages."""
    h = auth_headers('')
    r = requests.get(f'{HUB}/brainstorm/sessions/{session_id}?limit=50', headers=h, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def list_my_sessions() -> list[dict]:
    """List all sessions I'm participating in."""
    h = auth_headers('')
    r = requests.get(f'{HUB}/brainstorm/sessions', headers=h, timeout=10)
    if r.status_code == 200:
        return r.json().get('sessions', [])
    return []


# ── Core Loops ──────────────────────────────────────────────────────────────
async def heartbeat_loop():
    while True:
        await asyncio.sleep(20)
        p = json.dumps({'availability': 'online'}, separators=(',', ':'))
        h = {'Content-Type': 'application/json', **identity.get_auth_headers(p)}
        try:
            requests.post(f'{HUB}/registry/heartbeat', headers=h, data=p, timeout=5)
        except Exception:
            pass


async def listen():
    global conversation_counts
    reconnect_delay = 1
    while True:
        try:
            ts = str(int(time.time()))
            sig = identity.sign(NODE_ID, ts)
            uri = f'{WS}/ws/{NODE_ID}?timestamp={ts}&signature={urllib.parse.quote(sig)}'

            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                reconnect_delay = 1
                conversation_counts.clear()
                print(f'[{NODE_ALIAS}] Connected as {NODE_ID}', flush=True)

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    event = data.get('event', '?')
                    edata = data.get('data', {})

                    # ── Brainstorm Fanout ──────────────────────────────
                    if event == 'brainstorm_message':
                        session_id = edata.get('session_id')
                        topic = edata.get('topic', '')
                        message = edata.get('message', {})
                        sender = message.get('sender_id', '?')
                        content = message.get('content', '')
                        msg_id = message.get('message_id', '?')
                        participants = edata.get('participants', [])

                        # Track session
                        if session_id not in known_sessions:
                            known_sessions[session_id] = {'topic': topic, 'participants': participants}
                        else:
                            known_sessions[session_id]['participants'] = participants

                        print(f'[{NODE_ALIAS}] 🧠 [{session_id[:8]}] {sender[:20]}: {content[:150]}', flush=True)

                        # Don't reply to self
                        if sender == NODE_ID:
                            continue
                        # ANTI_LOOP: termination tokens
                        if '[END]' in content or '[NO_RELAY]' in content:
                            continue

                        # Generate and post reply
                        ctx = f'[Brainstorm session topic: {topic}]\n{sender} said: {content}\n\nGive a substantive 2-4 sentence technical response.'
                        reply = ai_reply(ctx)
                        post_to_session(session_id, reply, reply_to=msg_id)

                    # ── Standard DM ────────────────────────────────────
                    elif event == 'new_task':
                        tid = edata.get('id')
                        payload = edata.get('payload', '')
                        sender = edata.get('consumer_id')

                        if sender and sender != NODE_ID:
                            count = conversation_counts.get(sender, 0)
                            if count < MAX_EXCHANGES_PER_SENDER:
                                conversation_counts[sender] = count + 1
                                reply = ai_reply(f"[DM from {sender}]: {payload}")

                                cp = json.dumps({'task_id': tid, 'provider_id': NODE_ID, 'result_payload': reply})
                                requests.post(f'{HUB}/tasks/complete', headers=auth_headers(cp), data=cp, timeout=10)

                                rp = json.dumps({'consumer_id': NODE_ID, 'payload': reply, 'bounty': 0.0, 'target_node': sender})
                                requests.post(f'{HUB}/tasks/submit', headers=auth_headers(rp), data=rp, timeout=10)
                                print(f'[{NODE_ALIAS}] DM reply → {sender[:20]}', flush=True)
                            else:
                                cp = json.dumps({'task_id': tid, 'provider_id': NODE_ID, 'result_payload': '[BUDGET_EXHAUSTED]'})
                                requests.post(f'{HUB}/tasks/complete', headers=auth_headers(cp), data=cp, timeout=10)

        except websockets.exceptions.ConnectionClosed as e:
            print(f'[{NODE_ALIAS}] WS closed: {e.code}', flush=True)
        except Exception as e:
            print(f'[{NODE_ALIAS}] Error: {e}', flush=True)

        print(f'[{NODE_ALIAS}] Reconnecting in {reconnect_delay}s...', flush=True)
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


async def main():
    print(f'[{NODE_ALIAS}] Brainstorm-capable MEP listener', flush=True)
    print(f'[{NODE_ALIAS}] Node: {NODE_ID}', flush=True)
    await asyncio.gather(heartbeat_loop(), listen())


if __name__ == '__main__':
    asyncio.run(main())
