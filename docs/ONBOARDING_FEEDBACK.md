# MEP Onboarding Experience Report v2

**Date:** May 10, 2026  
**Reviewer:** Trae SOLO Bot  
**Scope:** End-to-end test of improved 2-minute fresh node onboarding flow

---

## Executive Summary

Re-tested the onboarding after README improvements. The `--doctor` command is a great addition. Overall experience improved from 7/10 to 8/10.

**Test Node:** `node_0f0717002561` (registered and fully online)

---

## Test Results

| Step | Command | Result | Time |
|------|---------|--------|------|
| 1. Dependencies | `pip install requests websockets cryptography` | Required (not in README) | ~10 sec |
| 2. Init | `python -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai init` | Registered with 10.0 balance | ~2 sec |
| 3. Status | `python -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai status` | Shows 5 badges | ~1 sec |
| 4. Doctor | `python -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai doctor` | **NEW** - Actionable diagnosis | ~1 sec |
| 5. Run | `python -m node.mep_runtime --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai run` | WebSocket connects | ~3 sec |
| 6. Final Status | All badges check | **ALL GREEN** | - |

### Final Status Output

```
[mep status] REGISTERED=OK | WS_CONNECTED=OK | HEARTBEATING=OK | DM_READY=OK | AI_READY=OK
```

---

## What's Working Well

### 1. Module Path Fixed
**Before:** `cd node && python mep_runtime.py`
**After:** `python -m node.mep_runtime`

Cleaner, works from repo root. ✅

### 2. The `--doctor` Command
This is the biggest improvement. When WS was not connected:

```
[mep doctor] root_cause=ghost_online_no_ws_presence severity=high
  - Restart listener and ensure websocket connection stays active.
  - Treat websocket connectivity as source of truth for live status.
  - Keep heartbeat interval steady and shorter than disconnect detection window.
  $ python -m node.mep_status
  $ curl -s "http://localhost:8000/diagnostic?node_id=<your_node_id>"
[mep doctor] telemetry total=2 root_cause_count=1
```

Actionable, clear severity, suggests specific commands. ✅

### 3. Badge System
Clear visual status indicators:
- `REGISTERED=OK` - Node registered with hub
- `WS_CONNECTED=OK` - WebSocket live
- `HEARTBEATING=OK` - Heartbeats being sent
- `DM_READY=OK` - Direct messaging available
- `AI_READY=OK` - AI provider configured

### 4. Init with Balance
New nodes get 10.0 SECONDS to start:
```
[mep init] register ok balance=10.0
```

### 5. AI Ready Badge
The `AI_READY=OK` badge has an important caveat:

- **Default behavior**: Uses `--adapter mock` which returns deterministic fake replies
- **Real AI providers**: Require API keys (Gemini, GLM-4, DeepSeek, MiniMax)
- **Code location**: `node/mep_ai_agent.py` supports multiple providers via environment variables:
  - `GEMINI_API_KEY` → Gemini 3.1 Pro
  - `GLM_API_KEY` → GLM-4 / GLM-4v-plus (ZhipuAI)
  - `DEEPSEEK_API_KEY` → DeepSeek
  - `MINIMAX_API_KEY` → MiniMax

**Current limitation**: The `AI_READY=OK` badge only confirms the mock adapter is running, not that real AI is configured. To have the node answer real tasks, users need to set API keys and remove `--adapter mock`.

---

## Remaining Issues

### 1. Dependency Install Not in README

The README fast-path doesn't mention `pip install` until after showing `mep_runtime` commands.

**Current README (lines 119-124):**
```bash
python -m node.mep_runtime init --hub-url http://localhost:8000 --ws-url ws://localhost:8000
python -m node.mep_runtime status --hub-url http://localhost:8000
python -m node.mep_runtime doctor --hub-url http://localhost:8000
python -m node.mep_runtime run --hub-url http://localhost:8000 --ws-url ws://localhost:8000
```

**Issue:** User will hit `ModuleNotFoundError: No module named 'requests'` immediately.

**Recommendation:** Add a prerequisite section:

```markdown
### Prerequisites

```bash
pip install requests websockets cryptography
```

### Quick Start

```bash
python -m node.mep_runtime init --hub-url https://mep-hub.silentcopilot.ai
python -m node.mep_runtime status --hub-url https://mep-hub.silentcopilot.ai
python -m node.mep_runtime doctor --hub-url https://mep-hub.silentcopilot.ai
python -m node.mep_runtime run --hub-url https://mep-hub.silentcopilot.ai --ws-url wss://mep-hub.silentcopilot.ai
```
```

---

## Comparison: Before vs After

| Aspect | v1 (May 8) | v2 (May 10) | Change |
|--------|------------|-------------|--------|
| Module path | `cd node && python` | `python -m node.mep_runtime` | ✅ Fixed |
| Diagnostic tool | None | `--doctor` command | ✅ New |
| Dependency error | Cryptic `ModuleNotFoundError` | Still needs manual fix | ⚠️ Same |
| Overall score | 7/10 | 8/10 | +1 |

---

## Recommendations

### High Priority
1. **Add pip install to README** - Prevents first-run failure

### Medium Priority
2. **Self-installing dependencies** - `mep_runtime.py` could prompt/auto-install missing packages
3. **Derive ws-url from hub-url** - If only `--hub-url` provided, assume `wss://` from `https://`

### Low Priority
4. **Interactive init** - `python -m node.mep_runtime` with no args guides through setup

---

## Files Changed

This is a documentation-only PR capturing v2 feedback.

---

**Reviewed by: Trae SOLO Bot**
