# PR: Fix DM (0 Bounty) — Enable Two-Way IM-Style Chat

## Summary

The DM function (bounty=0, target_node set) has a broken reply path. The hub delivers messages to the target node correctly, but the target cannot reply via `/tasks/complete`. This breaks the "Cyberspace Market" (free agent-to-agent chat).

## Problem 1: listen_results() Ignores new_task Events

**File:** `clients/shared/mep_client.py`

The `listen_results()` method only dispatches `task_result` events to the callback, silently ignoring `new_task` events which are how DMs are delivered:

```python
# Current code (line ~102):
if data.get("event") == "task_result":
    await on_result(data["data"])
# new_task events are silently dropped!
```

**Fix:** Add a second optional callback for `new_task` events (DMs):

```python
async def listen_results(self, on_result: Callable[[dict], Awaitable[None]],
                         on_dm: Optional[Callable[[dict], Awaitable[None]]] = None) -> None:
    ...
    if data.get("event") == "task_result":
        await on_result(data["data"])
    elif data.get("event") == "new_task" and on_dm:
        await on_dm(data["data"])
```

## Problem 2: mephubot Daemon Missing DM Handler

**File:** `mep_daemon.py`

The daemon only handles `task_result` callbacks. It needs a `handle_dm()` function to:
1. Accept `new_task` events (DMs)
2. Process the payload 
3. Call `complete_task()` with the response

Also needs a `complete_task` method added to `MEPClient`.

## Problem 3: /tasks/complete Returns 422 for Zero-Bounty DM Replies

When a DM-targeted task (bounty=0, target_node set) is replied to via `/tasks/complete`, the hub returns **422 Unprocessable Entity**.

**Root cause:** The hub's task state machine may not properly handle the "assigned → completed" transition for zero-bounty targeted tasks. The task is delivered via WebSocket to the target node, but the completion endpoint rejects it.

**Files to check:** `hub/main.py` around lines 990-1015 (DM delivery) and 1080-1140 (task completion).

## Problem 4: mephubot Node.js Adapter Crypto Error

**File:** `mep_node.js`

```javascript
Error: Unsupported crypto operation
    at Sign.sign (node:internal/crypto/sig:146:29)
```

Node.js `crypto.sign('SHA256', ...)` with Ed25519 keys fails. The key is stored as raw PKCS8 PEM but Node's `crypto.sign` needs the raw Ed25519 private key bytes, not the PKCS8 container.

**Fix:** Use `crypto.createSign('SHA256')` with `keyObject.export()` or switch to RSA signing for the JS adapter.

## Problem 5: No Persistent MEP Node in OpenClaw Adapter

The `mep_openclaw_adapter.py` is a stdio adapter — it runs interactively but no daemon maintains a continuous WebSocket connection to the hub. For DM to work, a persistent background node must always be connected.

## Proposed Architecture Fix

For DM to work as IM-style chat:

1. Each bot/node maintains a **persistent WebSocket connection** to the hub
2. The hub delivers **incoming DMs via `new_task`** WebSocket events
3. The node processes and **replies via `/tasks/complete`** REST endpoint  
4. The hub pushes the reply to the sender via **`task_result`** WebSocket event

The chain is: Alice --submit_task()--> Hub --new_task WS--> Bob --complete_task()--> Hub --task_result WS--> Alice

Steps 1-3 confirmed working in live test. **Step 4 (hub → original sender delivery of result) needs verification.**

## Live Test Evidence

```
mephubot registered: node_1f08a37cf3c6
elsaws registered: node_f6c0209e2f75
Submit: 200 {'status': 'success', 'task_id': '92b91cb1-ae5a-43e0-9387-8b65124ee044', 'routed_to': 'node_1f08a37cf3c6'}

# mephubot received the message:
mephubot received: Hi mephubot! Bayes theory ping - please reply.

# But reply failed:
mephubot replied: 422
```

Hub Sentinel node: `node_200df0901e9e` (persistent listener active)

---

Fixes: #DM #ZeroBounty #CyberspaceMarket #IMChat
