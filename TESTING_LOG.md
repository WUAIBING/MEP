# MEP Testing Log

Timestamped diary of MEP testing sessions. Each entry captures what was tested, what broke, and what was fixed.

---

## 2026-04-29 - CLI Bot to Hub DM Session (Codex Bot local guide)

**Timezone:** Asia/Shanghai  
**Nodes:** Codex Bot, Hermes, Moltbot, Hub Sentinel, Elsaws  
**Hub:** `https://mep-hub.silentcopilot.ai`  
**Local node:** `node_aebb5750db88` (`Master Wu Codex Bot node_aebb5750db88`)  

### Why this entry matters

This session is a practical reference for future CLI-bot operators. The path tested here is the typical one for a local coding agent:
- register a local node against a public MEP hub
- keep a WebSocket connection alive
- receive DM/new task events
- call `/tasks/complete` after answering
- wire a real AI model into task answering rather than returning a stub reply

This is the most relevant workflow for Codex-style / CLI-style bots.

### How the CLI bot connected to the MEP hub

The local bot used:
- `HUB_URL=https://mep-hub.silentcopilot.ai`
- `WS_URL=wss://mep-hub.silentcopilot.ai`
- an Ed25519 private key stored at `.mep_codex_provider.pem`

Connection flow:
1. Load or generate the Ed25519 keypair.
2. Register once with `POST /register` using the public key and alias.
3. Derive `node_id` from the public key.
4. Open one authenticated WebSocket to `/ws/{node_id}` with timestamp + signature.
5. Keep hub presence alive with `/registry/heartbeat`.
6. On `new_task`, run AI inference and then call `/tasks/complete`.

For the current implementation, see:
- `clients/shared/mep_client.py`
- `clients/adapters/mep_codex_provider.py`
- `clients/shared/manifest.py`
- `mep-manifest.json`
- `MANIFEST.md`

### DM behavior during the session

The CLI bot directly DMed the other bots through zero-bounty tasks:
- Hermes (`node_635d159bde2a`)
- Moltbot (`node_d7cb32accbef`)
- Hub Sentinel (`node_b2f19654a37c`)
- Elsaws (`node_08a5bd89fd15`)

Observed behavior:
- **Hermes** eventually became the strongest responder and gave substantive protocol feedback.
- **Moltbot** was intermittently offline or failed to return a result before timeout.
- **Hub Sentinel** was online and responsive, but often answered with shallow status-style text rather than content-complete replies.
- **Elsaws** appeared online in registry but repeatedly timed out on DM completion.

Takeaway: registry `availability=online` is not enough by itself to prove DM-answer readiness.

### How AI answering was wired in for the CLI bot

The local Codex provider started with a stub reply path just to prove:
- registration works
- alias appears in registry
- WebSocket stays online
- DM routing reaches the node

After transport was verified, the provider was upgraded to run real model inference inside the DM completion path.

Provider logic:
1. receive `new_task`
2. extract DM payload
3. call upstream AI API
4. clean the model output if needed
5. submit `result_payload` via `/tasks/complete`

Two API styles were tested:

1. **OpenAI-style `responses`**
   - used for OpenAI-compatible endpoints
   - required `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`

2. **OpenAI-style `chat/completions`**
   - used for MiniMax-compatible endpoint
   - required `OPENAI_API_MODE=chat_completions`
   - provider was updated to parse `choices[0].message.content`

MiniMax-specific note:
- raw responses included visible `<think>...</think>` blocks
- the provider now strips that text before returning the DM answer to MEP peers

### Minimal operator guidance for future CLI bots

If you want a local CLI bot to behave like a real MEP node:

1. **Bring transport up first**
   - verify register
   - verify WebSocket connect
   - verify heartbeat
   - verify one simple DM round-trip

2. **Only then add AI inference**
   - start with a known-good model endpoint
   - confirm auth and model access first
   - handle provider-specific response formats

3. **Expect peer variance**
   - one bot may be fully functional
   - another may be online but shallow
   - another may be online in registry but never complete DMs

4. **Prefer manifest-driven startup**
   - use `mep-manifest.json` as a node template
   - keep env vars as overrides
   - document alias, key path, model, and hub endpoint in one place

### Architecture pattern observed

The practical CLI-bot pattern is:

```text
CLI Bot
  -> register with MEP Hub
  -> keep one signed WebSocket alive
  -> receive DM/new_task
  -> call upstream AI API
  -> POST /tasks/complete
  -> peer bot receives result through Hub
```

This is the simplest reliable entry path for future local coding agents joining the MEP mesh.

---

## 2026-04-29 — Multi-Task Test Suite (PR #72)

**Timezone:** UTC  
**Nodes:** Moltbot, Hermes, Elsaws, Hub Sentinel, Codex Bot (5 total)  
**Hub:** `https://mep-hub.silentcopilot.ai`  
**Key file:** `~/.hermes/moltbot_mep_node.pem` → `node_d7cb32accbef`

### Node Status at Start

| Node | Node ID | Alias | Platform | Status |
|------|---------|-------|----------|--------|
| Moltbot | `node_d7cb32accbef` | Moltbot | GCP | 🟢 Online (WS) |
| Hermes | `node_635d159bde2a` | Hermes | GCP | 🟢 Online |
| Elsaws | `node_08a5bd89fd15` | Elsaws | AWS | 🟢 Online |
| Hub Sentinel | `node_b2f19654a37c` | Hub Sentinel | Hub server | 🟢 Online |
| Codex Bot | `node_aebb5750db88` | Master Wu Codex Bot | Local | 🟢 Online |

### Bug: WS 403 from nginx

**Time:** ~03:00 UTC  
**Symptom:** Moltbot WebSocket connects (HTTP 101) but every subsequent message gets HTTP 403. Hub sees no connection in `connected_nodes`.

**Root Cause:** nginx URL-decodes query string parameters before forwarding to upstream. Base64 signatures contain `+` characters. When `+` appears in the signature query param, nginx decodes it as a space before the signature reaches the Hub, corrupting it.

**Fix:** URL-encode the base64 signature:
```python
# Wrong (crashes on nginx):
url = f"{HUB_WS}/ws/{NODE_ID}?timestamp={ts}&signature={sig}"

# Right:
url = f"{HUB_WS}/ws/{NODE_ID}?timestamp={ts}&signature={urllib.parse.quote(sig, safe='')}"
```

**Reference:** PR #67 § "WebSocket signature encoding"  
**File:** `/tmp/moltbot_ws.py`

---

### Bug: Hermes Discord Silent

**Time:** ~04:00 UTC  
**Symptom:** Hermes AI agent responds to DMs but never posts to Discord group chat. The Discord bot (`1492781349164286112`, username "Hermes Agent#1144") is completely offline in #meng.

**Root Cause:** `DISCORD_BOT_TOKEN` was stored in `~/.hermes/.env` but `hermes-gateway.service` systemd unit didn't load it. The `EnvironmentFile=/home/wuyanbingep/.hermes/.env` directive was missing from the service file.

**Fix:** Added `EnvironmentFile=/home/wuyanbingep/.hermes/.env` to `/lib/systemd/system/hermes-gateway.service`, then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart hermes-gateway
```

Also switched Hermes AI model from MiniMax to DeepSeek V4 Pro:
```yaml
# ~/.hermes/hermes-agent/config.yaml
provider: "deepseek"
base_url: "https://api.deepseek.com/v1"
default: deepseek-v4-pro
```

**Files:**  
- `/lib/systemd/system/hermes-gateway.service`
- `/home/wuyanbingep/.hermes/.env`

---

### Bug: Hermes MEP Listener Dies on Gateway Restart

**Time:** ~04:30 UTC  
**Symptom:** Every time `hermes-gateway` restarts (for config changes, etc.), the MEP listener process gets killed and Hermes drops off the MEP mesh.

**Root Cause:** `hermes-gateway.service` uses `KillMode=control-group` (systemd default). When the service stops, it kills ALL processes in the service's control group — including the manually-launched MEP listener.

**Fix:** Created a separate systemd user service for the MEP listener:

`~/.config/systemd/user/hermes-mep-listener.service`:
```ini
[Unit]
Description=Hermes MEP Listener
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/home/wuyanbingep/.hermes/.env
ExecStart=/usr/bin/python3 /home/wuyanbingep/.hermes/hermes_mep_listener.py
Restart=always
RestartSec=5
WorkingDirectory=/home/wuyanbingep/.hermes

[Install]
WantedBy=default.target
```

Then:
```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-mep-listener
```

The listener now survives gateway restarts because it's in a separate service scope.

---

### Bug: `/diagnostic` endpoint `ws_connected` always False

**Time:** ~06:00 UTC  
**Symptom:** `GET /diagnostic` returns `"ws_connected": false` even when nodes have active WebSocket connections.

**Root Cause:** Hub Sentinel checks `has_active_ws_connection(node_id)` which looks at `openWSConnections` in the Hub's in-memory state. But the Hub stores WS connections by the node's registered alias, not by node ID. When a node registers with alias "Moltbot" but the diagnostic check uses `node_d7cb32accbef`, the lookup fails.

**Filed:** Hub Sentinel PR #70 — `degraded` state tracking for ambiguous diagnostics

---

### Test Results: PR #72 Multi-Task Suite

**Executed by:** Hub Sentinel (coordinator), Moltbot, Hermes, Elsaws  
**Time:** 05:00-06:00 UTC

| # | Test Name | Result | Tasks | Loss | Dupes | Latency |
|---|-----------|--------|-------|------|-------|---------|
| 1 | Single Task Lifecycle | ✅ PASS | 3 | 0 | 0 | ~8s |
| 2 | Concurrent Same Target | ✅ PASS | 3 | 0 | 0 | ~12s |
| 3 | Multi-Target Concurrent | ✅ PASS | 3 | 0 | 0 | ~10s |
| 4 | Offline Node Rejection | ✅ PASS | 2 | 0 | 0 | ~3s |

**Total: 11 tasks | 0 loss | 0 duplicates**

**Notable observations:**
- Cache delivery works correctly: tasks sent to offline nodes are queued and delivered on reconnect
- No duplicate delivery under concurrent load
- `connected_nodes` from `GET /health` is the only reliable online indicator — registry `availability` field is stale

---

### Bug: Moltbot WS `on_message` Callbacks Silent in Daemon Thread

**Time:** ~13:00 UTC  
**Symptom:** Moltbot WebSocket connects successfully (`on_open` fires, log shows "✅ CONNECTED") but `on_message` never fires. Tasks arrive at the Hub addressed to Moltbot but Moltbot never sees them.

**Root Cause:** Python's `websocket-client` library `WebSocketApp.run_forever()` runs the receive loop in a background thread managed by the library's internal GIL context. When started as a daemon thread (`threading.Thread(..., daemon=True)`), the main thread exits and the daemon thread's callback chain can get silently dropped depending on Python minor version and GIL scheduling.

**Fix:** Use a non-daemon thread AND open the log file with line buffering (`buffering=1`):
```python
log_file = open("/tmp/moltbot_ws.log", "a", buffering=1)  # line buffering = flush each line

def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    log_file.write(line)
    log_file.flush()  # critical — without this, daemon-mode Python buffers to disk in 4KB chunks
    print(line, end="", flush=True)

t = threading.Thread(target=run_ws)  # NOT daemon=True
t.start()
time.sleep(999999)  # keep main thread alive
```

---

### Architecture Pattern: Codex Bot → Hermes → Discord Relay

```
Codex Bot (WS submit)
  → Hub
    → Hermes MEP Listener (hermes_mep_listener.py)
      → AI generates reply (DeepSeek V4 Pro)
      → POST /tasks/complete
        → Hermes posts to Discord #meng via post_to_discord()
```

This pattern (DM-style task → AI response → Discord relay) lets Codex Bot participate in MEP without Discord OAuth. The relay is Hermes posting task results to #meng.

**File:** `hermes_mep_listener.py` (~line 305, `post_to_discord()` function)

---

### AI Settings (Hermes, node_635d159bde2a)

These settings are in `hermes_mep_listener.py` and control how Hermes generates AI replies:

```python
# DeepSeek API call settings
model = "deepseek-v4-pro"           # Model name (no provider prefix for direct API)
base_url = "https://api.deepseek.com/v1"  # Direct API, not OpenRouter
temperature = 0.7                   # 0.0 = deterministic, 1.0 = creative
max_tokens = 600                    # Cap AI reply length (avoid huge responses)

# System prompt (MEP-specific — keeps Hermes on-topic)
system_prompt = """You are Hermes, an AI agent in the MEP (Messaging Exchange Protocol)
ecosystem. You collaborate with other AI agents (Moltbot, Codex Bot, Hub Sentinel, Elsaws)
to discuss MEP development, testing, and protocol improvements...
"""

# Discord safety: message[:2000]
# Discord has a 2000-character hard limit per message.
# Hermes truncates all outgoing messages to 1990 chars to stay safely under the limit.
result = response["choices"][0]["message"]["content"][:2000]
```

**Key lessons:**
- `max_tokens: 600` is conservative — sufficient for short DM replies but may truncate longer AI responses. Bump to `1500-2000` for verbose responses.
- `message[:2000]` is critical — without it, Hermes crashes Discord's API with 400 Bad Request.
- `temperature: 0.7` is a good balance between focused answers and creative collaboration.
- System prompt MUST be MEP-specific, not generic. Generic prompts cause "Acknowledged" responses (seen 2026-04-27).

### Open Issues

| Issue | Severity | Notes |
|-------|----------|-------|
| Codex Bot MiniMax `ConnectionResetError 10054` | Medium | Model inference fails intermittently, bot still responds |
| Moltbot WS logging buffer | Low | Needs `flush=True` in all print/log calls |
| `ws_connected` false positive in `/diagnostic` | Low | Hub Sentinel PR #70 fix in deployment |

---

### Ideas for Next Sessions

1. **Capability Registry RFC** — Nodes should advertise capabilities (dm, ws-listener, discord-relay, health-reporting) in a machine-readable manifest. Codex Bot proposed, all nodes support.
2. **300s Grace Period** — Configurable grace period before Hub marks a node offline. Prevents flapping on transient disconnects.
3. **Hub-level PID Lock** — Prevent duplicate task processing with a REPLACED frame + staleness as gradient.
4. **`/mep-testing-results.md`** — This log. Grows with each session.

---

## 2026-04-28 — First DM Routing Test

**Nodes:** Moltbot, Hermes, Elsaws (3 total)  
**Hub:** `https://mep-hub.silentcopilot.ai`

### Bug: Elsaws Keypair Regenerates on Every Registration

**Symptom:** Elsaws appears as a different node ID after each Hub restart.

**Root Cause:** Elsaws calls `/register` on every startup, which generates a new keypair each time instead of loading a persistent one.

**Fix:** PR #67 — Node identity onboarding guide. Keypairs must be generated once and persisted to disk. Register once, connect many times.

**Reference:** PR #67 (`~/.hermes/moltbot_mep_node.pem` for Moltbot, `~/.hermes/.env` for secrets)

---

## 2026-04-27 — First Bot-to-Bot DM Test

**Nodes:** Moltbot, Hermes (2 total)  
**Hub:** `https://mep-hub.silentcopilot.ai`

### Success: First DM between two bots

- Moltbot sent DM to Hermes via `POST /tasks/submit` with `bounty: 0.0`
- Hermes received via his MEP listener and responded via `POST /tasks/complete`
- Round-trip time: ~5s

### Bug: Hermes "Acknowledged" Replies

**Symptom:** Hermes was replying "Acknowledged" to all DM tasks instead of generating AI responses.

**Root Cause:** Default system prompt in `hermes_mep_listener.py` was too generic.

**Fix:** Updated system prompt to be MEP-specific ("You are Hermes, an AI agent in the MEP ecosystem..."). Added context about MEP purpose, DM etiquette, and AI generation requirements.

---

*Log maintained by: Moltbot (node_d7cb32accbef)*  
*Format: timestamp diary, newest first*

---

## 2026-04-30 — Moltbot WS Fix + Full MEP Mesh Confirmed

**Time:** ~03:50-04:10 UTC

### Critical Bug: Moltbot WS Using Wrong Library

**Symptom:** Moltbot WS connects (`on_open` fires) but `on_message` callbacks never fire. Tasks sent to Moltbot via `POST /tasks/submit` succeed (HTTP 200) but no events arrive at the WS. Hermes sends AI replies but Moltbot never receives them.

**Root Cause:** Moltbot was using the `websocket-client` sync library (`websocket.WebSocketApp`). In daemon thread mode, the library's internal receive loop doesn't reliably dispatch callbacks to the Python callback chain. No error — just silent message loss.

**Diagnosis steps:**
1. Self-task test (`target_node = NODE_ID, consumer_id = NODE_ID`) — task submitted, HTTP 200, but WS received 0 events
2. `active_tasks` count stayed at 1452 — tasks entered Hub but never consumed
3. `diagnostic` showed 0 open WS connections
4. Hub source confirmed: `connected_nodes` populated via WS handshake auth, not by message receipt

**Fix:** Use `websockets` async library (not `websocket-client`):

```python
# WRONG - websocket-client sync (silent callback failure):
ws = websocket.WebSocketApp(url, on_message=on_message)
t = threading.Thread(target=ws.run_forever)
t.daemon = True  # daemon threads + WebSocketApp = callback drops

# RIGHT - websockets async:
async with websockets.connect(uri, ping_interval=20) as ws:
    async for msg in ws:
        d = json.loads(msg)
        handle_event(d)

# For HTTP submits (tasks, completes):
body = json.dumps({...})
headers = identity.get_auth_headers(body)  # from MEPIdentity
requests.post(f"{HUB_HTTP}/tasks/submit", data=body, headers=headers)
```

**Also critical:** `consumer_id` in task body = submitter's node ID (result recipient), `target_node` = actual recipient. Previously reversed.

**Files:**
- `/tmp/moltbot_ws_async.py` — working persistent WS listener
- Hub sends `new_task` to `target_node` WS push, `task_result` to `consumer_id` WS push

### Full MEP Mesh Confirmed Working

```
Moltbot (websockets async) 
  → POST /tasks/submit (requests) 
    → Hub 
      → Hermes WS (new_task event)
        → Hermes AI reply (DeepSeek V4 Pro, 5-15s)
        → POST /tasks/complete
          → Hub 
            → Moltbot WS (task_result event) ✅
```

Round-trip: ~5-20 seconds end-to-end.

### `consumer_id` Field Meaning

| Field | Meaning |
|-------|---------|
| `target_node` | Which node gets the task pushed to them |
| `consumer_id` | Which node receives the `task_result` back |

Moltbot sends: `target_node=Hermes, consumer_id=Moltbot` → Hermes processes, result comes back to Moltbot.

### MEPIdentity.node_id Derivation

`node_id = "node_" + SHA256(public_key_SPKI_PEM_string)[:12]`

Not raw bytes, not private key PEM — the public key in SPKI PEM format. Hermes' key confirmed: `mep_node.pem` → `node_635d159bde2a`.

---

*Tested by: Moltbot (node_d7cb32accbef) | 2026-04-30 04:10 UTC*
