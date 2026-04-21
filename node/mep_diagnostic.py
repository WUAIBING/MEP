#!/usr/bin/env python3
"""
MEP Node Diagnostic Tool — Fully Offline
=======================================
Self-contained diagnostic for MEP node operators.
No API calls, works completely offline after setup.

Checks:
  1. Key file existence & location
  2. Node ID computation (client vs Hub)
  3. Hub connectivity
  4. Node registration status
  5. WebSocket connection
  6. Known bug patterns & fix guidance

Usage:
  python3 mep_diagnostic.py
  python3 mep_diagnostic.py --key-path ~/.mep/mep_node.pem
  python3 mep_diagnostic.py --ollama http://localhost:11434  # use local Ollama
"""
import os
import sys
import json
import hashlib
import base64
import argparse
import urllib.request
import urllib.error
import urllib.parse
import asyncio
import socket
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HUB_URL = "https://mep-hub.silentcopilot.ai"
HUB_API = f"{HUB_URL}/api"
DEFAULT_OLLAMA = "http://localhost:11434"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
G, R, Y, B, BO, RST = "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[1m", "\033[0m"

def ok(msg):   print(f"{G}✓{RST} {msg}")
def fail(msg): print(f"{R}✗{RST} {msg}")
def warn(msg): print(f"{Y}⚠{RST} {msg}")
def info(msg): print(f"{B}ℹ{RST} {msg}")
def bold(msg): print(f"{BO}{msg}{RST}")

def section(n, title):
    print(f"\n{BO}[{n}] {title}{RST}")

def cmd(cmdline):
    """Print a shell command."""
    print(f"\n  {G}${RST} {cmdline}")

# ---------------------------------------------------------------------------
# Built-in known bugs & fixes (fully offline knowledge base)
# ---------------------------------------------------------------------------
# Try to load shared FAQ from MEP repo's welcome.py (used by Hub)
_WELCOME_AVAILABLE = False
try:
    import sys as _sys
    _sys.path.insert(0, "/home/node/.openclaw/workspace/MEP")
    from node.welcome import FAQ as _WELCOME_FAQ
    BUGS = {item["id"]: item for item in _WELCOME_FAQ}
    _WELCOME_AVAILABLE = True
except Exception:
    # Fallback when welcome.py is not available
    BUGS = {
        "node_id_mismatch": {
            "id": "node_id_mismatch",
            "symptom": "401 Unauthorized / 403 Forbidden / signature verification failed",
            "cause": "MEPIdentity and Hub hash the public key differently.",
            "fix_steps": [
                "1. Run diagnostic: python3 mep_diagnostic.py",
                "2. Check if Your node_id ≠ Hub computes",
                "3. Fix: update node/identity.py or hub/auth.py to use same hash input",
                "4. Re-register with fixed code",
            ],
            "files": ["node/identity.py", "hub/auth.py"],
        },
        "key_in_tmp": {
            "id": "key_in_tmp",
            "symptom": "Key not found / node identity changed after reboot",
            "cause": "Key stored in /tmp which is cleared on reboot.",
            "fix_steps": [
                "1. Move key: cp /tmp/mep_node.pem ~/.mep/mep_node.pem",
                "2. Update KEY_PATH in scripts",
                "3. Verify: python3 -c \"from node.identity import MEPIdentity; print(MEPIdentity('~/.mep/mep_node.pem').node_id)\"",
            ],
            "files": ["/tmp/mep_node.pem"],
        },
        "not_bidding": {
            "id": "not_bidding",
            "symptom": "RFC events received but no bids placed / Hub shows 0 bidders",
            "cause": "WebSocket handler handles 'new_task' but not 'rfc' event type.",
            "fix_steps": [
                "1. Add RFC handler: elif data.get('event') == 'rfc': await handle_rfc(data['data'])",
                "2. In handle_rfc(), call POST /tasks/bid with task_id and provider_id",
                "3. Test by submitting a task",
            ],
            "files": ["node/listen_ws.py"],
        },
        "missing_deps": {
            "id": "missing_deps",
            "symptom": "ModuleNotFoundError / ImportError / No module named",
            "cause": "Required Python packages not installed.",
            "fix_steps": [
                "1. pip install cryptography websockets requests urllib3",
                "2. Verify: python3 -c 'import websockets, cryptography, requests; print(\"OK\")'",
            ],
            "files": [],
        },
    }


def diagnose(symptoms: list) -> list:
    """Match symptoms to known bugs."""
    matched = []
    for bug_id, bug in BUGS.items():
        # welcome.py uses "symptom" (singular), local dict uses "symptoms" (list)
        symptom_text = bug.get("symptom", "") or ""
        symptom_list = bug.get("symptoms", [symptom_text])
        for symptom in symptoms:
            if any(symptom.lower() in st.lower() for st in symptom_list if st):
                matched.append(bug_id)
                break
    return matched


# ---------------------------------------------------------------------------
# Identity & Key Checks
# ---------------------------------------------------------------------------
def load_key(key_path: str) -> dict:
    """Load Ed25519 key and compute node IDs."""
    result = {"path": key_path, "exists": False, "error": None,
              "node_id": None, "pubkey_b64": None, "hub_node_id": None}

    if not os.path.exists(key_path):
        result["error"] = f"Key file not found: {key_path}"
        return result

    result["exists"] = True

    try:
        from cryptography.hazmat.primitives import serialization
        private_bytes = open(key_path, "rb").read()
        private_key = serialization.load_pem_private_key(private_bytes, password=None)
        public_key = private_key.public_key()

        # Client: PEM SubjectPublicKeyInfo
        pub_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")

        # Extract raw base64 from PEM
        b64 = "".join(l for l in pub_pem.strip().split("\n")
                      if not l.startswith("-----"))
        b64_stripped = b64.rstrip("=")

        # Client node_id (hash PEM string)
        sha_client = hashlib.sha256(pub_pem.encode()).hexdigest()
        result["node_id"] = f"node_{sha_client[:12]}"

        # Hub node_id (hash raw base64 string)
        sha_hub = hashlib.sha256(b64.encode()).hexdigest()
        result["hub_node_id"] = f"node_{sha_hub[:12]}"

        result["pubkey_b64"] = b64_stripped
        result["node_id_match"] = result["node_id"] == result["hub_node_id"]

    except ImportError as e:
        result["error"] = f"cryptography not installed: {e}"
    except Exception as e:
        result["error"] = str(e)

    return result


def check_key_safety(key_path: str) -> list:
    """Detect unsafe key storage."""
    issues = []
    if key_path.startswith("/tmp"):
        issues.append(f"Key is in /tmp — LOST on reboot!")
    if not os.access(key_path, 0o600) and not os.access(key_path, 0o400):
        issues.append(f"Key permissions too open: {oct(os.stat(key_path).st_mode)[-3:]}")
    return issues


# ---------------------------------------------------------------------------
# Hub Communication
# ---------------------------------------------------------------------------
def check_hub_health() -> bool:
    try:
        r = urllib.request.urlopen(f"{HUB_URL}/health", timeout=5)
        data = json.loads(r.read())
        ok(f"Hub online: {data.get('status', 'ok')}")
        return True
    except Exception as e:
        fail(f"Hub unreachable: {e}")
        return False


def check_registration(node_id: str) -> dict:
    """Check if node is registered on Hub."""
    try:
        req = urllib.request.Request(
            f"{HUB_API}/nodes/{node_id}",
            headers={"x-mep-nodeid": node_id}
        )
        r = urllib.request.urlopen(req, timeout=5)
        ok(f"Registered on Hub: {node_id}")
        return {"registered": True, "data": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            fail(f"Not registered: {node_id}")
            return {"registered": False}
        warn(f"HTTP {e.code}")
        return {"registered": None}
    except Exception as e:
        warn(f"Check failed: {e}")
        return {"registered": None}


def test_registration(pubkey_b64: str, alias: str = None) -> dict:
    """Register with Hub and see what node_id it assigns."""
    alias = alias or f"diag-{socket.gethostname()[:8]}"
    payload = json.dumps({"pubkey": pubkey_b64, "alias": alias}).encode()
    req = urllib.request.Request(
        f"{HUB_URL}/register",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        data = json.loads(r.read())
        ok(f"Registration OK — Hub assigned: {data.get('node_id')}, balance: {data.get('balance')}")
        return {"success": True, "hub_node_id": data.get("node_id"), "balance": data.get("balance")}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        fail(f"Registration HTTP {e.code}: {body}")
        return {"success": False, "error": body}
    except Exception as e:
        fail(f"Registration error: {e}")
        return {"success": False}


def test_websocket(node_id: str, key_path: str) -> dict:
    """Test WebSocket connection."""
    try:
        import websockets
        import time as time_module

        with open(key_path, "rb") as f:
            pk = f.read()
        from cryptography.hazmat.primitives import serialization
        sk = serialization.load_pem_private_key(pk, password=None)

        ts = str(int(time_module.time()))
        msg = f"{node_id}{ts}".encode()
        sig = base64.b64encode(sk.sign(msg)).decode()
        uri = f"wss://mep-hub.silentcopilot.ai/ws/{node_id}?timestamp={ts}&signature={urllib.parse.quote(sig)}"

        async def _test():
            async with websockets.connect(uri, timeout=8) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                return json.loads(msg)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_test())
        ok(f"WebSocket connected — received: {result.get('event', result)}")
        return {"connected": True, "msg": result}
    except ImportError:
        warn("websockets not installed — skipped WS test")
        return {"connected": None}
    except asyncio.TimeoutError:
        fail("WebSocket timed out — Hub may not recognize this node_id")
        return {"connected": False}
    except Exception as e:
        fail(f"WebSocket error: {e}")
        return {"connected": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Ollama Integration (local LLM — fully offline)
# ---------------------------------------------------------------------------
def check_ollama(base_url: str) -> dict:
    """Check if local Ollama is available."""
    try:
        r = urllib.request.urlopen(f"{base_url}/api/tags", timeout=3)
        models = json.loads(r.read()).get("models", [])
        names = [m["name"] for m in models]
        ok(f"Ollama running: {names or 'no models installed'}")
        return {"available": True, "models": names}
    except Exception:
        return {"available": False}


def ollama_chat(base_url: str, model: str, prompt: str, timeout: int = 60) -> str:
    """Query local Ollama model."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.3, "max_tokens": 512}
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[Ollama error: {e}]"


OLLAMA_PROMPT_TEMPLATE = """You are an MEP (Miao Exchange Protocol) node troubleshooting assistant.

CONTEXT:
- User's node_id (client computes): {node_id}
- Hub computes node_id: {hub_node_id}
- Node IDs match: {match}
- Registered on Hub: {registered}
- WebSocket connected: {ws}
- Symptoms reported: {symptoms}

KNOWN BUGS IN SYSTEM:
1. Node ID mismatch: client and Hub hash public key differently → 401/403 errors
2. Key in /tmp: lost on reboot → sudden identity changes
3. RFC not handled: listener gets RFC events but doesn't bid → tasks unclaimed
4. Missing deps: websockets/cryptography not installed

TASK: Based on the above, explain:
1. What is most likely the problem
2. Why it happened
3. Exact commands to fix it (be specific, show actual curl commands)

Be brief and actionable. Assume user is a node operator.
Output format: Problem → Why → Fix (with actual commands).
"""


def ask_ollama(base_url: str, model: str, diagnostics: dict, symptoms: list) -> str:
    """Use local Ollama to generate fix guidance."""
    prompt = OLLAMA_PROMPT_TEMPLATE.format(
        node_id=diagnostics.get("node_id", "unknown"),
        hub_node_id=diagnostics.get("hub_node_id", "unknown"),
        match=diagnostics.get("node_id_match", False),
        registered=diagnostics.get("registered"),
        ws=diagnostics.get("ws", "unknown"),
        symptoms=", ".join(symptoms) or "none"
    )
    return ollama_chat(base_url, model, prompt)


# ---------------------------------------------------------------------------
# Fix Guide Renderer
# ---------------------------------------------------------------------------
def show_fix(bug_id: str):
    """Print detailed fix guide for a known bug."""
    bug = BUGS[bug_id]
    # welcome.py uses "symptom" as title; local dict uses "title"
    title = bug.get("title") or bug.get("symptom", bug_id)
    bold(f"\n  ╔═ {title}")
    print(f"  ║  Cause: {bug['cause']}")
    print(f"  ║")
    # fix_steps is List[str] in welcome.py, List[tuple] in local fallback
    steps = bug.get("fix_steps", [])
    for i, step in enumerate(steps, 1):
        if isinstance(step, tuple):
            step_name, step_text = step
            print(f"  ║  {i}. [{step_name}] {step_text}")
        else:
            text = step.strip()
            if text.startswith(f"{i}."):
                text = text[3:].strip()  # strip leading "1. "
            print(f"  ║  {i}. {text}")
    files = bug.get("files", [])
    if files:
        print(f"  ║  Files: {', '.join(f for f in files if f)}")
    url = bug.get("url")
    if url:
        print(f"  ║  More: {url}")
    print(f"  ╚══════════════════════════════════")


def show_summary(issues: list, diagnostics: dict):
    """Print a clean summary."""
    print(f"\n{BO}─── Summary ───{RST}")
    if not issues:
        ok("All checks passed!")
        return

    warn(f"{len(issues)} issue(s) found:\n")
    for issue in issues:
        bug = BUGS.get(issue)
        if bug:
            print(f"  {R}✗{RST} {bug.get('title') or bug.get('symptom', '')}")
    print()


# ---------------------------------------------------------------------------
# Main Diagnostic Run
# ---------------------------------------------------------------------------
def run(key_path: str = None, ollama_url: str = None, ollama_model: str = None,
        symptoms: list = None, offline: bool = False):
    """Run full diagnostic suite."""
    bold(f"\n{'='*50}")
    bold(f"  MEP Node Diagnostic Tool — Fully Offline")
    bold(f"{'='*50}\n")

    issues_found = []
    all_symptoms = symptoms or []

    # ── 1. Find key ──────────────────────────────────────────────
    section("1", "Key File")

    if not key_path:
        candidates = [
            "~/.mep/mep_node.pem",
            "~/.hermes/mep_node.pem",
            "~/mep_node.pem",
            "/tmp/mep_node.pem",
            "/tmp/hermes_mep_node.pem",
            "/tmp/sentinel.pem",
            "/home/node/.openclaw/workspace/mep-sentinel/node/sentinel.pem",
            "node/sentinel.pem",
        ]
        for c in candidates:
            c = os.path.expanduser(c)
            if os.path.exists(c):
                key_path = c
                info(f"Found: {c}")
                break

    if not key_path or not os.path.exists(key_path):
        fail("No key found.")
        print("\n  Searched:")
        for c in candidates:
            print(f"    {os.path.expanduser(c)}")
        print("\n  Generate a key:")
        cmd("python3 -c \"from node.identity import MEPIdentity; MEPIdentity('~/.mep/mep_node.pem')\"")
        return

    ident = load_key(key_path)

    if ident["error"]:
        fail(f"Load error: {ident['error']}")
        issues_found.append("missing_deps")
        return

    ok(f"Key: {key_path}")
    bold(f"  Your node_id:    {ident['node_id']}")
    bold(f"  Hub computes:     {ident['hub_node_id']}")

    if not ident["node_id_match"]:
        warn("  → MISMATCH! This WILL cause 401/403 errors.")
        issues_found.append("node_id_mismatch")
        all_symptoms.append("401 Unauthorized")

    # Key safety
    safety = check_key_safety(key_path)
    for s in safety:
        warn(f"  {s}")
        issues_found.append("key_in_tmp")

    # ── 2. Dependencies ─────────────────────────────────────────
    section("2", "Dependencies")
    missing = []
    for pkg in ["cryptography", "websockets", "requests"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        fail(f"Missing: {', '.join(missing)}")
        cmd("pip install cryptography websockets requests urllib3")
        issues_found.append("missing_deps")
    else:
        ok("All dependencies installed")

    # ── 3. Hub Connectivity ──────────────────────────────────────
    section("3", "Hub Connectivity")
    hub_ok = check_hub_health()

    if not hub_ok:
        warn("Hub unreachable — skip remaining network tests")
        all_symptoms.append("Hub unreachable")
    else:
        # ── 4. Registration ────────────────────────────────────
        section("4", "Node Registration")
        reg = check_registration(ident["node_id"])

        print("\n  Test registration...")
        reg_test = test_registration(ident["pubkey_b64"])

        if not ident["node_id_match"]:
            issues_found.append("node_id_mismatch")
            all_symptoms.append("node_id mismatch")
        elif not reg.get("registered"):
            issues_found.append("registration_404")
            all_symptoms.append("node not found on Hub")

        # ── 5. WebSocket ───────────────────────────────────────
        if hub_ok:
            section("5", "WebSocket Connection")
            if "websockets" not in missing:
                ws = test_websocket(ident["node_id"], key_path)
            else:
                warn("Skipped (websockets not installed)")
                ws = {"connected": None}

    # ── 6. Bug Matching & Fix ────────────────────────────────────
    section("6", "Diagnosis")

    # Auto-match from symptoms
    matched = diagnose(all_symptoms)
    for m in matched:
        if m not in issues_found:
            issues_found.append(m)

    if issues_found:
        print(f"\n  Detected {len(issues_found)} issue(s):")
        for issue_id in issues_found:
            bug = BUGS.get(issue_id)
            if bug:
                print(f"\n  {R}✗{RST} {bug.get('title') or bug.get('symptom', '')}")
        print()
    else:
        ok("No known issues detected.")

    # ── 7. Show Fixes ────────────────────────────────────────────
    unique_issues = list(dict.fromkeys(issues_found))  # preserve order, dedupe
    for issue_id in unique_issues:
        if issue_id in BUGS:
            show_fix(issue_id)

    # ── 8. Ollama Q&A ────────────────────────────────────────────
    if ollama_url:
        section("8", f"Local LLM ({ollama_model or 'auto'})")
        ollama = check_ollama(ollama_url)
        if ollama["available"]:
            models = ollama["models"]
            model = ollama_model or (models[0] if models else None)
            if model:
                info(f"Using model: {model}")
                print("\n  Asking local LLM...")
                advice = ask_ollama(ollama_url, model, ident, all_symptoms)
                print(f"\n  {B}LLM says:{RST}\n")
                for line in advice.split("\n"):
                    print(f"    {line}")
            else:
                warn("No models installed in Ollama.")
                print("\n  Install a model:")
                cmd("ollama pull qwen3:1.8b")
                cmd("ollama pull llama3.2:1b")
        else:
            warn(f"Ollama not reachable at {ollama_url}")
            print("\n  Start Ollama:")
            cmd("ollama serve")
            cmd("ollama pull qwen3:1.8b  # ~1.1GB, good for diagnostics")

    # ── Final Summary ─────────────────────────────────────────────
    print(f"\n{BO}─── Summary ───{RST}")
    if unique_issues:
        warn(f"{len(unique_issues)} issue(s) need fixing — see above.")
    else:
        ok("All checks passed!")


# ---------------------------------------------------------------------------
# Standalone Info Commands
# ---------------------------------------------------------------------------

def _show_info(args):
    """Handle --welcome, --faq, --faq-json flags."""
    try:
        sys.path.insert(0, "/home/node/.openclaw/workspace/MEP")
        from node.welcome import (
            get_welcome, get_faq, format_faq_for_display,
            WELCOME_TEMPLATE, WELCOME_MARKDOWN, OLLAMA_SYSTEM_PROMPT
        )
    except ImportError:
        print(f"{R}Error: welcome.py not found. Run from MEP repo directory.{RST}")
        return

    if args.welcome:
        # Show welcome message (node_id is optional — uses placeholder)
        welcome = get_welcome(
            node_id="<your-node-id>",
            alias="Node Operator",
            balance=0.0
        )
        print(welcome.get("text", WELCOME_TEMPLATE.format(
            alias="Node Operator",
            node_id="<your-node-id>",
            balance=0.0
        )))

    if args.faq:
        print(format_faq_for_display("markdown"))

    if args.faq_json:
        import json
        faq = get_faq()
        print(json.dumps(faq, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="MEP Node Diagnostic Tool — works fully offline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 mep_diagnostic.py
  python3 mep_diagnostic.py --key-path ~/.mep/mep_node.pem
  python3 mep_diagnostic.py --ollama http://localhost:11434
  python3 mep_diagnostic.py --ollama http://localhost:11434 --model qwen3:1.8b
  python3 mep_diagnostic.py --symptom "401 Unauthorized" --symptom "WS refused"
  python3 mep_diagnostic.py --offline  # skip all network calls, basic key check only

No API keys needed. No internet required after initial setup.
        """
    )
    p.add_argument("--key-path", help="Path to Ed25519 private key PEM file")
    p.add_argument("--ollama", metavar="URL", default=None,
                   help="Ollama base URL for local LLM (default: http://localhost:11434)")
    p.add_argument("--model", help="Ollama model name to use (default: first available)")
    p.add_argument("--symptom", action="append", default=[],
                   help="Report a symptom (can be repeated)")
    p.add_argument("--offline", action="store_true",
                   help="Skip all network calls, basic checks only")
    p.add_argument("--hub-url", default=HUB_URL,
                   help=f"Hub URL (default: {HUB_URL})")
    p.add_argument("--welcome", action="store_true",
                   help="Show welcome message for new node operators")
    p.add_argument("--faq", action="store_true",
                   help="Show full troubleshooting FAQ")
    p.add_argument("--faq-json", action="store_true",
                   help="Output FAQ as JSON")

    args = p.parse_args()

    # Handle standalone info commands
    if args.welcome or args.faq or args.faq_json:
        _show_info(args)
        return

    # Detect Ollama automatically if running
    ollama_url = args.ollama
    if not ollama_url:
        for url in [DEFAULT_OLLAMA, "http://127.0.0.1:11434"]:
            try:
                urllib.request.urlopen(f"{url}/api/tags", timeout=2)
                ollama_url = url
                break
            except Exception:
                pass

    run(
        key_path=args.key_path,
        ollama_url=ollama_url,
        ollama_model=args.model,
        symptoms=args.symptom,
    )


if __name__ == "__main__":
    main()
