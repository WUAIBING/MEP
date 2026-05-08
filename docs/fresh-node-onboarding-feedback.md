# Fresh Node Onboarding Feedback

## Test Date: 2026-05-08

## Test Environment
- Machine: DO droplet (DigitalOcean)
- OS: Linux
- MEP Version: Latest (main branch after git pull)

## Test Commands Executed

```bash
# 1. Initialize (Fresh Node)
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai init

# 2. Check Status
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai status

# 3. Run Doctor
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai doctor

# 4. Start Node
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai run
```

## Results

| Step | Expected | Actual | Status |
|------|----------|--------|--------|
| init | Register node | node_c350e9b042ab, balance=10.0 | ✅ PASS |
| status | Show badges | REGISTERED=OK, WS_CONNECTED=FAIL | ⚠️ PARTIAL |
| doctor | Diagnostics | 404 Not Found | ❌ FAIL |
| run | Connect WS | WS still shows FAIL | ❌ FAIL |

## Findings

### What Works ✅
1. **init command** - Successfully creates identity and registers node
2. **Status badges** - Shows REGISTERED, WS_CONNECTED, HEARTBEATING, DM_READY, AI_READY
3. **CLI interface** - Clean argparse interface with good error messages

### Issues Found ❌

#### 1. WebSocket Connection Not Established
- **Issue:** After running `mep_runtime run`, status still shows `WS_CONNECTED=FAIL`
- **Root cause:** The MockAdapter doesn't implement WebSocket connection logic
- **Expected behavior:** Should connect to WebSocket and receive tasks

**Suggested fix:** Add WebSocket connection to MockAdapter or document that it only responds to HTTP tasks.

#### 2. Doctor Endpoint Not Found (404)
- **Issue:** `mep_runtime doctor` returns 404 for `/onboard/diagnose`
- **Root cause:** Hub at `mep-hub.silentcopilot.ai` doesn't implement the `/onboard/diagnose` endpoint
- **Expected:** Hub should implement the diagnose endpoint per PR #120 design

**Suggested fix:** Add `/onboard/diagnose` endpoint to hub/main.py, or document which hub version supports it.

### Code Quality: Already Good
- Type hints throughout
- Proper error handling
- Clean CLI design
- Tests included

## Recommendations

### Immediate (Hub-side)
1. Add `/onboard/diagnose` endpoint
2. Implement WebSocket server in runtime or document HTTP-only mode

### Documentation
1. Document that `mock` adapter is HTTP-only
2. List hub requirements (which endpoints need to be implemented)

### Future
1. Add real-time status updates
2. Add reconnection logic for WS drops

## Testing Tips Used
```bash
# Quick fresh node test commands that worked:
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai init

# Test without full setup (dry-run mode)
python3 -m node.mep_runtime --hub-url http://localhost:8000 status
```

## Verdict
The new runtime is a **significant improvement** over the old manual process. The CLI makes it much easier to get started. However, hub-side support needs to catch up to fully enable the 2-minute onboarding vision.

## Test Output Log

```
[Elsaws on DO]
$ python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai init
[mep init] node_id=node_c350e9b042ab
[mep init] generated key=/home/node/.mep/mep_runtime.pem
[mep init] register ok balance=10.0
[mep status] REGISTERED=OK | WS_CONNECTED=FAIL | HEARTBEATING=OK | DM_READY=FAIL | AI_READY=OK

$ python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai doctor
[mep doctor] diagnose failed status=404 detail={"detail":"Not Found"}
```

---
*Tested by Elsaws (ICE GOD) via OpenClaw on DigitalOcean*