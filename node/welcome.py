"""
MEP Node Welcome & Troubleshooting System
========================================
Provides structured welcome messages and FAQ for newly registered MEP nodes.
Designed for:
  1. Hub to serve via /register response
  2. Local Ollama to parse and present beautifully to node operators
  3. Node operators to self-diagnose common issues

No external API required — works fully offline with local Ollama.

Usage:
    from node.welcome import get_welcome, get_faq, format_for_ollama

    # Get full welcome package for a new node
    welcome = get_welcome(node_id, alias)

    # Get troubleshooting FAQ
    faq = get_faq()

    # Format for local Ollama chat
    ollama_prompt = format_for_ollama(node_id, alias, balance)
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from typing import List, Optional


# ---------------------------------------------------------------------------
# Welcome Message
# ---------------------------------------------------------------------------

WELCOME_TEMPLATE = """
🎉 Welcome to MEP, {alias}!

Your node is now registered as `{node_id}`.

📊 Your Dashboard:
   Balance : {balance} SECONDS
   Node ID : {node_id}
   Hub URL : https://mep-hub.silentcopilot.ai
   WebSocket: wss://mep-hub.silentcopilot.ai/ws/{node_id}

🚀 Quick Start:
   1. Keep this script running to stay online
   2. Listen for RFC events → place bids → earn SECONDS
   3. Run the diagnostic tool: python3 node/mep_diagnostic.py

📚 Documentation:
   README.md         — Full protocol documentation
   OPERATOR_CHECKLIST.md — Setup checklist for node operators
   node/             — Provider scripts (mep_provider.py, etc.)

💬 Troubleshooting:
   Run the diagnostic tool for auto-detection:
     python3 node/mep_diagnostic.py --ollama http://localhost:11435

   Or ask your local Ollama:
     python3 node/mep_diagnostic.py --ollama http://localhost:11435 --model llama3.2:1b

🆘 Common Issues:
   • "401 Unauthorized" → Node ID mismatch (see FAQ #1)
   • "Connection refused" → Hub unreachable or wrong URL
   • "Key not found" → Key stored in /tmp was lost (see FAQ #2)

Happy mining! ⛏️
"""

WELCOME_MARKDOWN = """
# 🎉 Welcome to MEP, {alias}!

You are now connected to the **Miao Exchange Protocol**.

## Your Node

| Field | Value |
|-------|-------|
| **Node ID** | `{node_id}` |
| **Balance** | {balance} SECONDS |
| **Hub URL** | `https://mep-hub.silentcopilot.ai` |
| **WebSocket** | `wss://mep-hub.silentcopilot.ai/ws/{node_id}` |

## What Happens Next

Your node will automatically:
1. Maintain a WebSocket connection to the Hub
2. Receive RFC (Request for Citation) events when tasks are available
3. Place bids to compete for tasks
4. Process tasks and earn SECONDS

## Diagnostic Tool

For self-service troubleshooting, run:

```bash
# Install Ollama (one-time setup)
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3.2:1b

# Run the diagnostic tool
python3 node/mep_diagnostic.py \\
    --key-path ~/.mep/mep_node.pem \\
    --ollama http://localhost:11435 \\
    --model llama3.2:1b
```

## Common Issues

|Q#|Symptom|Cause|Solution|
|---|--------|-----|--------|
|1|401/403 Auth Failure|Node ID mismatch|Recompute node_id from key|
|2|Key regenerated on reboot|Key stored in /tmp|Move to persistent storage|
|3|No bids being placed|Listener doesn't handle RFC events|Add bid handler to WebSocket loop|
|4|WS connection refused|Hub down or firewall|Verify Hub health + firewall rules|

## Need Help?

- Run: `python3 node/mep_diagnostic.py`
- Issues: https://github.com/Hub-Sentinel/MEP/issues
- Protocol docs: `docs/` directory
"""


# ---------------------------------------------------------------------------
# Troubleshooting FAQ
# ---------------------------------------------------------------------------

@dataclass
class FAQItem:
    id: str
    symptom: str
    cause: str
    fix_steps: List[str]
    files: List[str]
    severity: str  # "critical", "warning", "info"

FAQ = [
    FAQItem(
        id="node_id_mismatch",
        symptom="401 Unauthorized / 403 Forbidden / HTTP 403 / HTTP 401 / 'signature verification failed'",
        cause=(
            "MEPIdentity (client) and the Hub hash the public key differently. "
            "MEPIdentity hashes the PEM-encoded public key string. "
            "The Hub hashes the raw base64 public key string. "
            "Result: two different node_ids for the same key pair. "
            "Your client tries to authenticate with node_id_A, but the Hub registered you as node_id_B."
        ),
        fix_steps=[
            "1. Identify: Run diagnostic — check if 'Your node_id' ≠ 'Hub computes'",
            "2. Get Hub's node_id: POST /register with your pubkey → response contains 'node_id'",
            "3. Fix Option A (change client): In node/identity.py, compute node_id by hashing the raw base64 (not PEM string), OR",
            "4. Fix Option B (change Hub): In hub/auth.py, change derive_node_id() to hash the PEM string like the client does",
            "5. Re-register: After fixing, call POST /register to get a new node_id",
            "6. Update all scripts: Replace old node_id with the new one in your config",
        ],
        files=["node/identity.py", "hub/auth.py"],
        severity="critical",
    ),
    FAQItem(
        id="key_in_tmp",
        symptom="Key file not found / Node re-registered with new identity / 'Unknown Node ID'",
        cause=(
            "The private key PEM file was stored in /tmp, which is cleared on system reboot, "
            "by tmpfs conversion, or by system cleanup tools (bleachbit, etc.). "
            "A new key was generated, giving the node a completely new identity. "
            "The old node_id is now orphaned on the Hub."
        ),
        fix_steps=[
            "1. Move key to persistent location: mkdir -p ~/.mep && cp /tmp/mep_node.pem ~/.mep/mep_node.pem",
            "2. Update KEY_PATH in your listener: export MEP_KEY_PATH=~/.mep/mep_node.pem",
            "3. Verify identity: python3 -c \"from node.identity import MEPIdentity; print(MEPIdentity('~/.mep/mep_node.pem').node_id)\"",
            "4. Re-register if needed: POST /register with your pubkey",
            "5. Add to startup: Ensure the key exists before the listener starts",
        ],
        files=["/tmp/mep_node.pem", "/tmp/hermes_mep_node.pem"],
        severity="critical",
    ),
    FAQItem(
        id="not_bidding",
        symptom="RFC events received / no /tasks/bid call / Hub shows 0 bids / Tasks timeout unclaimed",
        cause=(
            "The WebSocket listener receives RFC (Request for Citation) events from the Hub, "
            "but the code only handles 'new_task' events — not 'rfc' events. "
            "The node never calls POST /tasks/bid, so the Hub sees zero bids and the task goes unclaimed."
        ),
        fix_steps=[
            "1. Find your WebSocket handler: grep -n 'event.*new_task' node/listen_ws.py",
            "2. Add RFC handler: elif data.get('event') == 'rfc': await handle_rfc(data['data'])",
            "3. Implement handle_rfc():",
            "   task_id = data['data']['id']",
            "   payload = json.dumps({'task_id': task_id, 'provider_id': node_id}, separators=(',', ':'))",
            "   headers = get_auth_headers(payload)",
            "   requests.post(f'{HUB_URL}/tasks/bid', data=payload, headers=headers)",
            "4. Test: Submit a task, watch logs for 'Bid result: accepted'",
        ],
        files=["node/listen_ws.py", "node/mep_provider.py"],
        severity="warning",
    ),
    FAQItem(
        id="missing_deps",
        symptom="ModuleNotFoundError / ImportError / 'No module named websockets'",
        cause="Required Python packages (cryptography, websockets, requests) not installed in the node's Python environment.",
        fix_steps=[
            "1. Install dependencies: pip install cryptography websockets requests urllib3",
            "2. Verify: python3 -c 'import websockets, cryptography, requests; print(\"All OK\")'",
            "3. If using venv: source venv/bin/activate && pip install -r requirements.txt",
        ],
        files=["requirements.txt"],
        severity="info",
    ),
    FAQItem(
        id="hub_unreachable",
        symptom="Connection refused / ConnectionError / Cannot connect to wss://mep-hub.silentcopilot.ai",
        cause="Hub is down, network/firewall blocking outbound 443, or WebSocket URL is incorrect.",
        fix_steps=[
            "1. Check Hub status: curl https://mep-hub.silentcopilot.ai/health",
            "2. Check your firewall: ensure outbound TCP 443 is allowed",
            "3. Verify WebSocket URL format: wss://mep-hub.silentcopilot.ai/ws/{node_id}",
            "4. Check proxy settings: unset HTTP_PROXY HTTPS_PROXY",
        ],
        files=[],
        severity="warning",
    ),
    FAQItem(
        id="registration_404",
        symptom="404 on /api/nodes/{node_id}/heartbeat / 'node not found on Hub'",
        cause="The node_id used by the client doesn't match any registered node on the Hub (usually from the node_id_mismatch bug).",
        fix_steps=[
            "1. Re-register: curl -X POST https://mep-hub.silentcopilot.ai/register "
             "-H 'Content-Type: application/json' "
             "-d '{\"pubkey\": \"<base64-pubkey>\", \"alias\": \"my-node\"}'",
            "2. Use the 'node_id' from the Hub response in ALL subsequent API calls",
            "3. Update local identity: ensure node/identity.py uses the Hub-returned node_id",
        ],
        files=["node/identity.py"],
        severity="critical",
    ),
    FAQItem(
        id="ws_connection_closed",
        symptom="WebSocket connection closed unexpectedly / ConnectionClosed / ping timeout",
        cause="Hub restarted, network instability, or Hub kicked the node due to missed heartbeats.",
        fix_steps=[
            "1. Implement automatic reconnection with exponential backoff in your WebSocket loop",
            "2. Send HTTP heartbeat to /registry/heartbeat every 30s as backup",
            "3. Example reconnection: except ConnectionClosed: await asyncio.sleep(2**attempt); attempt += 1",
            "4. Check Hub health: curl https://mep-hub.silentcopilot.ai/health",
        ],
        files=["node/listen_ws.py"],
        severity="info",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_welcome(node_id: str, alias: str, balance: float = 0.0) -> dict:
    """Get the full welcome package for a newly registered node."""
    return {
        "text": WELCOME_TEMPLATE.format(
            alias=alias or "Node Operator",
            node_id=node_id,
            balance=balance,
        ),
        "markdown": WELCOME_MARKDOWN.format(
            alias=alias or "Node Operator",
            node_id=node_id,
            balance=balance,
        ),
        "node_id": node_id,
        "alias": alias,
        "balance": balance,
    }


def get_faq() -> List[dict]:
    """Get all troubleshooting FAQ items as dicts."""
    return [asdict(item) for item in FAQ]


def format_faq_for_display(style: str = "markdown") -> str:
    """Format FAQ for terminal/markdown display."""
    if style == "text":
        lines = ["\n" + "=" * 60 + "\nMEP Node Troubleshooting FAQ\n" + "=" * 60 + "\n"]
        for item in FAQ:
            lines.append(f"\n[{item.severity.upper()}] Q: {item.symptom}")
            lines.append(f"    Cause: {item.cause}")
            for step in item.fix_steps:
                lines.append(f"    {step}")
            if item.files:
                lines.append(f"    Files: {', '.join(item.files)}")
        return "\n".join(lines)
    else:
        lines = ["# MEP Node Troubleshooting FAQ\n"]
        for item in FAQ:
            sev_emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(item.severity, "•")
            lines.append(f"\n## {sev_emoji} {item.symptom}")
            lines.append(f"\n**Cause:** {item.cause}\n")
            lines.append("**Fix:**")
            for step in item.fix_steps:
                lines.append(f"- {step}")
            if item.files:
                lines.append(f"\n**Files:** `{'`, `'.join(item.files)}`")
        return "\n".join(lines)


OLLAMA_SYSTEM_PROMPT = """You are MEP Node Assistant — a friendly, expert guide for Miao Exchange Protocol node operators.

RULES:
- Always be helpful, patient, and concise
- Speak plainly — no jargon without explanation
- Prioritize critical issues (401/403, key loss) before minor ones
- Give exact commands to copy-paste
- When unsure, ask for the diagnostic output

TROUBLESHOOTING METHOD:
1. Identify the symptom (what does the user see?)
2. Find the root cause (why did it happen?)
3. Provide exact fix commands (what to run?)
4. Confirm with verification step (how to know it worked?)

You have access to the node's diagnostic output. Ask for it if not provided."""


def format_for_ollama(node_id: str, alias: str, balance: float = 0.0) -> str:
    """Format welcome + FAQ as a conversation for local Ollama."""
    faq_md = format_faq_for_display("markdown")
    return f"""你是 MEP 节点助手。请用欢迎信息和故障排除指南帮助新注册节点。

欢迎信息:
{WELCOME_TEMPLATE.format(alias=alias or "Node Operator", node_id=node_id, balance=balance)}

故障排除FAQ:
{faq_md}

如果节点遇到问题，先问他们运行了什么命令、看到了什么错误信息。
"""


def get_all_as_json() -> str:
    """Dump all welcome/FAQ data as JSON for programmatic use."""
    return json.dumps({
        "welcome_template": WELCOME_TEMPLATE,
        "welcome_markdown": WELCOME_MARKDOWN,
        "ollama_system_prompt": OLLAMA_SYSTEM_PROMPT,
        "faq": get_faq(),
    }, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="MEP Welcome & FAQ Generator")
    p.add_argument("--node-id", default="node_example123")
    p.add_argument("--alias", default="My Node")
    p.add_argument("--balance", type=float, default=0.0)
    p.add_argument("--format", default="text", choices=["text", "markdown", "json", "ollama"])
    p.add_argument("--faq-only", action="store_true")

    args = p.parse_args()

    if args.faq_only:
        print(format_faq_for_display(args.format))
    elif args.format == "json":
        print(get_all_as_json())
    elif args.format == "markdown":
        print(WELCOME_MARKDOWN.format(alias=args.alias, node_id=args.node_id, balance=args.balance))
        print("\n---\n")
        print(format_faq_for_display("markdown"))
    elif args.format == "ollama":
        print(format_for_ollama(args.node_id, args.alias, args.balance))
    else:
        print(WELCOME_TEMPLATE.format(alias=args.alias, node_id=args.node_id, balance=args.balance))
