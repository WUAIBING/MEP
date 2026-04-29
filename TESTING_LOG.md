# MEP Testing Log

Timestamped diary of MEP testing sessions. Each entry captures what was tested, what broke, and what was fixed.

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
