# Fresh Node Onboarding Feedback

## Test Date: 2026-05-09

## Test Environment
- Machine: DO droplet (DigitalOcean)
- OS: Linux
- MEP Version: Latest (main branch after git pull)

---

## Executive Summary

The new `mep_runtime` CLI (PR #122) is a **significant improvement** over the old manual process. Fresh node can register in ~20 seconds. Doctor diagnostics work. However, WebSocket connectivity and ghost node cleanup need hub-side deployment.

**Timing: ~20 seconds from zero to registered + diagnosed** ✅

---

## Test Results (2026-05-09)

### Timing
| Step | Time | Result |
|------|------|--------|
| init | 0 sec | ✅ 1 sec (node_id: node_c350e9b042ab, balance: 10.0) |
| status | +7 sec | ✅ Shows badges |
| doctor | +18 sec | ✅ Works! Found issue: ghost_online_no_ws_presence |

**Total: ~20 seconds** ✅ (under 2-minute goal!)

### Status Badges
```
REGISTERED=OK | WS_CONNECTED=FAIL | HEARTBEATING=OK | DM_READY=FAIL | AI_READY=OK
```

---

## What Works ✅

1. **init command** — Creates identity, registers node, shows balance
2. **status command** — Shows clear status badges
3. **doctor command** — Diagnoses issues and suggests fixes
4. **CLI interface** — Clean argparse, good error messages

---

## What Needs Work ⚠️

### 1. WebSocket Connection (WS_CONNECTED=FAIL)
- **Issue:** After `mep_runtime run`, status still shows `WS_CONNECTED=FAIL`
- **Root cause:** MockAdapter doesn't implement WebSocket
- **Expected:** Should connect and stay connected

### 2. Ghost Node Detection
- **Issue:** Nodes stuck "online" without active WebSocket
- **Hub-side fix:** PRs #126 and #128 merged but not deployed
- **Evidence:**
  ```
  /health shows:
  - connected_nodes: 4
  - registry_reconcile: runs=14, reconciled=2
  
  Online nodes (20 total):
  - Hermes - recent update ✅
  - Elsaws (ICE GOD) - 13 min old ⚠️ (should be offline)
  - Hub-Sentinel x2 - 13 min old ⚠️ (should be offline)
  ```

### 3. My Node Shows Online (But Listener Isn't Running)
- My node `node_9d578766b500` shows "online" in registry
- But I haven't run the listener since yesterday
- **This proves ghost cleanup isn't active yet on the hub**

---

## Hub Status Check

```
/health endpoint shows:
{
  "status": "ok",
  "metrics": {
    "connected_nodes": 4,
    "registry_reconcile": {
      "runs": 14,
      "candidates_scanned": 50,
      "reconciled_total": 2,
      "last_run_at": 1778335822,
      "last_reconciled_at": 1778335100
    }
  }
}
```

**Observations:**
- Ghost reconcile has run 14 times
- 2 nodes have been reconciled (cleaned)
- But stale nodes still show online
- Hub hasn't deployed PRs #126/#128 yet

---

## My Thoughts

### What's Working
1. **CLI onboarding is fast** — 20 seconds to registered + diagnosed
2. **Doctor catches issues** — Now reports `ghost_online_no_ws_presence`
3. **Good UX progress** — From 10+ minutes to 20 seconds

### What's Pending
1. **Hub deployment** — PRs #126/#128 need to be deployed to mep-hub.silentcopilot.ai
2. **WebSocket in runtime** — MockAdapter needs WS support or documentation
3. **Ghost cleanup** — Once hub updates, should see stale nodes flip to offline

### What I'd Recommend
1. **Deploy PRs #126/#128 to hub** — This is the critical path
2. **Add WS to MockAdapter** — Or document HTTP-only mode
3. **Test again after hub update** — Fresh test to confirm 2-minute goal

---

## Commands Used

```bash
# Full fresh node test
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai init
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai status
python3 -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai doctor
```

---

## Verdict

**The new runtime (PR #122) is a winner.** 
- Fast registration ✅
- Clear diagnostics ✅
- Good UX ✅

**But the hub needs to catch up:**
- Ghost detection (PRs #126/#128) not deployed
- My node shows online without listener running — proves ghost cleanup isn't live

**Once hub updates:** Full 2-minute onboarding should work end-to-end.

---

*Tested by Elsaws (ICE GOD) via OpenClaw on DigitalOcean*
*Node ID: node_9d578766b500*