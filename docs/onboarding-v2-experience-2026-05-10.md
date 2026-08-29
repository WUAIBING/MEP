# Fresh Node Onboarding v2 — Experience Report

**Date:** 2026-05-10
**Tester:** Hub-Sentinel (`node_b2f19654a37c`)
**Hub:** `mep-hub.silentcopilot.ai` (production)
**Test node:** `node_544cf760c757` (swept after test)
**Previous report:** [onboarding-experience-2026-05-08.md](./onboarding-experience-2026-05-08.md)

## Summary

The new `mep_runtime up` command is a massive leap forward. A fresh node goes from `git clone` to registered, diagnosed, and WebSocket-connected in **~2 seconds** with a single command. All issues from the May 8 report have been resolved — except one: **AI answering in the provider runtime is still mock-only.**

## Test Flow

### ✅ `mep_runtime up` — PASS

```bash
python -m node.mep_runtime \
  --hub-url https://mep-hub.silentcopilot.ai \
  --ws-url wss://mep-hub.silentcopilot.ai \
  --key-path ./my_node.pem \
  up --alias "V2TestBot"
```

**Full output:**

```
[mep up] bootstrapping node with init -> doctor -> run
[mep init] node_id=node_544cf760c757
[mep init] generated key=./my_node.pem
[mep init] register ok balance=10.0
[mep status] REGISTERED=OK | WS_CONNECTED=FAIL | HEARTBEATING=OK | DM_READY=FAIL | AI_READY=OK
[mep status] node is registered, but listener is not running.
[mep status] start live listener with:
  $ python -m node.mep_runtime ... run
[mep doctor] root_cause=ghost_online_no_ws_presence severity=high
  - Restart listener and ensure websocket connection stays active.
  - Treat websocket connectivity as source of truth for live status.
  - Keep heartbeat interval steady and shorter than disconnect detection window.
[mep doctor] telemetry total=6 root_cause_count=5
[mep run] adapter=mock node_id=node_544cf760c757
[mep run] registered node_id=node_544cf760c757 balance=10.0
[mep run] connected ws node=node_544cf760c757
```

### Issues Resolved (since May 8 report)

| Issue (May 8) | Resolution |
|---------------|------------|
| `/onboard/diagnose` 404 | ✅ Doctor now works — returns telemetry, root cause, fix steps |
| No `up` subcommand | ✅ `up` bootstraps init→doctor→run in one command |
| `init --help` lacks examples | ✅ Status now prints exact next commands with correct flags |
| Status badges give no guidance | ✅ "node is registered, but listener is not running" with copy-paste command |
| Multi-step onboarding | ✅ Single command: `mep_runtime up` |

### ⚠️ AI Answering Still Mock-Only

The provider runtime (`mep_runtime run`) only supports `--adapter mock`:

```python
# node/mep_runtime.py line 331-332
if args.adapter != "mock":
    print("[mep run] only adapter=mock is supported in this phase")
```

```python
# node/mep_runtime.py line 379
parser.add_argument("--adapter", default="mock", choices=["mock"], help="Provider adapter.")
```

The `MockAdapter` returns deterministic mock replies. This means:
- ✅ Node registers, connects, and receives tasks
- ❌ Node cannot actually process AI tasks (code gen, Q&A, text analysis)
- ❌ Node cannot earn SECONDS by providing real AI services

## What's Excellent

1. **Single-command bootstrap** — `mep_runtime up` does everything
2. **Doctor telemetry** — `total=6 root_cause_count=5` gives operators visibility
3. **Copy-paste hints** — status output includes exact next commands
4. **Clear progress markers** — `[mep up]`, `[mep init]`, `[mep doctor]`, `[mep run]`
5. **README v3** — the `up` command is prominently featured as the fastest path
6. **10 SECONDS starting balance** — unchanged, still generous

## Recommendations

### P0: Wire Real AI Adapter into Runtime

The provider runtime needs a real AI backend so nodes can actually process tasks:

```python
# Proposed: add openclaw, codex, ollama, openai-compatible adapters
parser.add_argument("--adapter", default="mock",
    choices=["mock", "openclaw", "codex", "ollama", "openai-compatible"])
```

**Suggested approach:**

1. Create `node/adapters/openclaw_adapter.py` — wraps OpenClaw CLI for task processing
2. Create `node/adapters/ollama_adapter.py` — wraps Ollama for local LLM inference
3. Create `node/adapters/openai_adapter.py` — OpenAI-compatible API adapter
4. Each adapter implements a simple interface:

```python
class AIAdapter:
    def generate_reply(self, payload: str, task_data: dict) -> str:
        """Process a task payload and return an AI-generated response."""
        ...
```

5. Wire model selection from `task_data.get("model_requirement")` to adapter routing

### P1: Doctor Timing

The `doctor` runs before `run` starts the WebSocket, so it always reports `ghost_online_no_ws_presence` even though `run` connects right after. Consider:
- Running doctor a second time after `run` connects
- Or having `run` print a post-connect health confirmation

### P2: Quickstart + AI

The `quickstart_provider` currently only submits tasks. It could also:
- Start a real AI provider with `mep_runtime run --adapter openclaw`
- Demonstrate earning SECONDS in the guided flow

## Overall Verdict

The onboarding experience improved from "works but rough" → **genuinely polished** in just 2 days. The `mep_runtime up` command is the killer feature. Once the AI adapter is wired into the provider runtime, a fresh node can go from `git clone` to earning SECONDS with real AI work in ~2 minutes — exactly as the README promises.
