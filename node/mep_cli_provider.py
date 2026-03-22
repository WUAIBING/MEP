#!/usr/bin/env python3
"""
MEP CLI Provider
A specialized node that routes tasks to local autonomous CLI agents
(e.g., Aider, Claude-Code, Open-Interpreter).
"""
from typing import Optional
import asyncio
import websockets
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import os
import shlex
import aiohttp
import urllib.parse
import time
import tempfile
from identity import MEPIdentity

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")
WS_URL = os.getenv("WS_URL", "wss://mep-hub.silentcopilot.ai")

class MEPCLIProvider:
    def __init__(self, key_path: str):
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.balance = 0.0
        self.is_contributing = True
        self.capabilities = ["cli-agent", "bash", "python"]
        self.session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        self.workspace_dir = os.path.join(tempfile.gettempdir(), "mep_workspaces")
        os.makedirs(self.workspace_dir, exist_ok=True)
        self.upload_code = os.getenv("MEP_CLI_UPLOAD_CODE", "false").lower() in ("1", "true", "yes")
        self.max_code_chars = int(os.getenv("MEP_CLI_MAX_CODE_CHARS", "12000"))
        # Safety: maximum SECONDS a node will spend to buy data (negative bounty)
        self.max_purchase_price = float(os.getenv("MEP_MAX_PURCHASE_PRICE", "0.0"))

    async def _post_with_retry(self, url: str, payload_str: str | None = None, json_body: dict | None = None, headers: dict | None = None, timeout: int = 20):
        delays = [1, 2, 4, 8]
        for i, delay in enumerate(delays, start=1):
            try:
                if json_body is not None:
                    return await asyncio.to_thread(self.session.post, url, json=json_body, timeout=(5, timeout))
                return await asyncio.to_thread(self.session.post, url, data=payload_str, headers=headers, timeout=(5, timeout))
            except Exception as e:
                print(f"[CLI Provider] Request attempt {i} failed: {e}")
                if i == len(delays):
                    print(f"[CLI Provider] Request failed: {e}")
                    return None
                await asyncio.sleep(delay)
    async def connect(self):
        """Connect to MEP Hub and start listening for CLI tasks."""
        print(f"[CLI Provider {self.node_id}] Starting...")
        
        try:
            print(f"[CLI Provider] Registering with hub: {HUB_URL}")
            resp = await self._post_with_retry(f"{HUB_URL}/register", json_body={"pubkey": self.identity.pub_pem}, timeout=10)
            if resp is None:
                print("[CLI Provider] Registration failed: no response from hub")
                return
            self.balance = resp.json().get("balance", 0.0)
            print(f"[CLI Provider] Registered. Balance: {self.balance:.6f} SECONDS")
        except Exception as e:
            print(f"[CLI Provider] Registration failed: {e}")
            return
            
        ts = str(int(time.time()))
        sig = self.identity.sign(self.node_id, ts)
        sig_safe = urllib.parse.quote(sig)
        uri = f"{WS_URL}/ws/{self.node_id}?timestamp={ts}&signature={sig_safe}"
        try:
            async with websockets.connect(uri) as ws:
                print("[CLI Provider] Connected to MEP Hub. Awaiting CLI tasks...")
                while self.is_contributing:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        data = json.loads(msg)
                        
                        if data["event"] == "new_task":
                            await self.process_task(data["data"])
                        elif data["event"] == "rfc":
                            await self.handle_rfc(data["data"])
                        elif data["event"] == "task_result":
                            await self.handle_task_result(data["data"])
                            
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        print("[CLI Provider] Connection closed")
                        break
        except Exception as e:
            print(f"[CLI Provider] WebSocket error: {e}")

    async def handle_rfc(self, rfc_data: dict):
        """Evaluate if we should bid on this CLI task."""
        task_id = rfc_data["id"]
        bounty = rfc_data["bounty"]
        model = rfc_data.get("model_requirement")
        
        if model not in self.capabilities and model is not None:
            return
        
        # 🛡️ CRITICAL SAFETY CHECK: DO NOT BUY DATA UNLESS EXPLICITLY ENABLED
        if bounty < 0:
            cost = abs(bounty)
            if cost > self.max_purchase_price:
                print(f"[CLI Provider] 🚨 REJECTED DATA MARKET TASK {task_id[:8]}: "
                      f"Price {cost:.6f} SECONDS exceeds max_purchase_price {self.max_purchase_price:.6f}")
                return
            else:
                print(f"[CLI Provider] ✅ Accepting data purchase {task_id[:8]} for {cost:.6f} SECONDS "
                      f"(max allowed: {self.max_purchase_price:.6f})")
        
        print(f"[CLI Provider] Received matching RFC {task_id[:8]} for {bounty:.6f} SECONDS. Bidding...")
        
        try:
            payload_str = json.dumps({
                "task_id": task_id,
                "provider_id": self.node_id
            })
            headers = self.identity.get_auth_headers(payload_str)
            headers["Content-Type"] = "application/json"
            resp = await self._post_with_retry(f"{HUB_URL}/tasks/bid", payload_str=payload_str, headers=headers)
            
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                if data["status"] == "accepted":
                    print(f"[CLI Provider] BID WON! Executing CLI agent for task {task_id[:8]}...")
                    task_data = {
                        "id": task_id,
                        "payload": data["payload"],
                        "bounty": bounty,
                        "consumer_id": data["consumer_id"],
                        "payload_uri": data.get("payload_uri")
                    }
                    # Run it in background so we don't block the websocket
                    # Fetch the secret_data from Hub after winning bid
                    secret_data = data.get("secret_data")
                    asyncio.create_task(self.process_task(task_data, secret_data=secret_data))
        except Exception as e:
            print(f"[CLI Provider] Error placing bid: {e}")

    async def _fetch_secret_data(self, task_id: str) -> Optional[str]:
        """Fetch the secret_data from Hub for a Data Market purchase."""
        try:
            headers = self.identity.get_auth_headers("")
            headers["Content-Type"] = "application/json"
            resp = requests.get(f"{HUB_URL}/tasks/result/{task_id}", headers=headers, timeout=10)
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                return data.get("result_payload")
        except Exception as e:
            print(f"[CLI Provider] Failed to fetch secret data: {e}")
        return None

    def _payload_is_message(self, payload: str) -> bool:
        """Determine if payload is a message vs executable code.
        
        Heuristics for message detection:
        1. No code blocks (```)
        2. No shell commands (starts with $, >, etc.)
        3. Contains natural language markers
        4. Short length for simple DMs
        """
        if not payload or not isinstance(payload, str):
            return False
            
        payload = payload.strip()
        
        # Code blocks indicate executable code
        if "```" in payload:
            return False
            
        # Shell/command indicators
        if payload.startswith(("$", ">", "#!", "python", "bash", "sh", "curl", "wget")):
            return False
            
        # Very long payloads might be code/data
        if len(payload) > 1000:
            return False
            
        # Check for common code patterns
        code_patterns = [
            "def ", "class ", "import ", "from ", "if __name__",
            "function(", "const ", "let ", "var ", "print(",
            "return ", "for ", "while ", "async ", "await "
        ]
        for pattern in code_patterns:
            if pattern in payload:
                return False
                
        # Default to message for safety (better to treat as message than execute)
        return True

    async def _handle_dm(self, dm_data: dict):
        """Handle direct messages (0 bounty tasks) by writing to inbox."""
        import json
        import time
        import datetime
        
        inbox_entry = {
            "time": time.time(),
            "datetime": datetime.datetime.now().isoformat(),
            "task_id": dm_data.get("id", dm_data.get("task_id", "unknown")),
            "consumer_id": dm_data.get("consumer_id", "unknown"),
            "bounty": dm_data.get("bounty", 0),
            "payload": dm_data.get("payload", "")
        }
        
        inbox_file = "inbox.jsonl"
        with open(inbox_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(inbox_entry) + "\n")
        
        print(f"[CLI Provider] 💌 DM received from {dm_data.get('consumer_id', 'unknown')}")
        print(f"  Task ID: {inbox_entry['task_id']}")
        print(f"  Message: {dm_data.get('payload', '')[:80]}...")

    async def handle_task_result(self, result_data: dict):
        """Handle task_result events (results from tasks we submitted)."""
        import json
        import time
        import datetime
        
        task_id = result_data.get("task_id", "unknown")
        provider_id = result_data.get("provider_id", "unknown")
        result_payload = result_data.get("result_payload", "")
        
        print("[CLI Provider] 🎉 TASK RESULT RECEIVED!")
        print(f"  Task ID: {task_id}")
        print(f"  From Provider: {provider_id}")
        print(f"  Result: {result_payload[:100]}...")
        
        # Save result to results file
        result_entry = {
            "time": time.time(),
            "datetime": datetime.datetime.now().isoformat(),
            "task_id": task_id,
            "provider_id": provider_id,
            "result_payload": result_payload
        }
        
        results_file = "results.jsonl"
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(result_entry) + "\n")
        
        print(f"  ✅ Result saved to {results_file}")

    async def process_task(self, task_data: dict, secret_data: Optional[str] = None):
        """Execute the task using a local CLI agent."""
        task_id = task_data["id"]
        payload = task_data["payload"]
        bounty = task_data["bounty"]
        
        # ===== DM DETECTION: Handle 0-bounty messages as DMs =====
        if bounty == 0 and self._payload_is_message(payload):
            await self._handle_dm(task_data)
            return
        # ===== END DM DETECTION =====
        
        # If this is a Data Market purchase, save the secret data!
        if secret_data:
            task_dir = os.path.join(self.workspace_dir, task_id)
            os.makedirs(task_dir, exist_ok=True)

            data_file = os.path.join(task_dir, "purchased_data.txt")
            with open(data_file, "w", encoding="utf-8") as f:
                f.write(secret_data)
            print(f"[CLI Provider] 💾 Saved purchased data to {data_file}")
        
        payload_uri = task_data.get("payload_uri")
        
        try:
            
            task_dir = os.path.join(self.workspace_dir, task_id)
            os.makedirs(task_dir, exist_ok=True)
            
            # If there's an IPFS or HTTP payload, download it first!
            if payload_uri:
                print(f"[CLI Provider] 📥 Downloading massive payload from {payload_uri}...")
                dl_url = payload_uri
                if payload_uri.startswith("ipfs://"):
                    dl_url = payload_uri.replace("ipfs://", "https://ipfs.io/ipfs/")
                try:
                    async with aiohttp.ClientSession(trust_env=True) as session:
                        async with session.get(dl_url) as resp:
                            if resp.status == 200:
                                file_path = os.path.join(task_dir, "downloaded_payload.bin")
                                with open(file_path, "wb") as f:
                                    f.write(await resp.read())
                                print(f"[CLI Provider] ✅ Downloaded payload to {file_path}")
                                payload += f"\n[Note: Large payload downloaded to {file_path}]"
                            else:
                                print(f"[CLI Provider] ❌ Failed to download payload: HTTP {resp.status}")
                except Exception as e:
                    print(f"[CLI Provider] ❌ Download error: {e}")

            print(f"[CLI Provider] Upload code enabled: {self.upload_code}")
            
            if os.name == "nt":
                safe_payload = payload.replace('"', '""')
                cmd = (
                    "echo Booting Autonomous CLI Agent... & "
                    "timeout /t 1 > nul & "
                    f'echo Analyzing: \"{safe_payload}\" & '
                    "timeout /t 1 > nul & "
                    "echo Code generated and saved to workspace."
                )
            else:
                safe_payload = shlex.quote(payload)
                cmd = (
                    "echo 'Booting Autonomous CLI Agent...' && "
                    "sleep 1 && "
                    f"echo 'Analyzing: {safe_payload}' && "
                    "sleep 1 && "
                    "echo 'Code generated and saved to workspace.'"
                )
            agent_cmd = os.getenv("MEP_CLI_AGENT_CMD")
            if agent_cmd:
                if "{payload}" in agent_cmd:
                    # Substitute placeholder with quoted payload
                    cmd = agent_cmd.replace("{payload}", safe_payload)
                else:
                    # Append quoted payload to agent command
                    cmd = f"{agent_cmd} {safe_payload}"
                # Note: Full fix requires subprocess_exec + arg array instead of shell=True
                # to prevent injection via agent_cmd path itself.
            
            print(f"\n[CLI Agent] Executing in {task_dir}:")
            print(f"$ {cmd[:100]}...\n")
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=task_dir
            )
            
            stdout, stderr = await process.communicate()
            
            output = stdout.decode(errors="replace").strip()
            if stderr:
                output += "\n[Errors/Warnings]:\n" + stderr.decode(errors="replace").strip()
                
            print(f"[CLI Agent] Finished with exit code {process.returncode}")
            code_block = ""
            if self.upload_code:
                py_candidates = [
                    os.path.join(task_dir, name)
                    for name in os.listdir(task_dir)
                    if name.lower().endswith(".py")
                ]
                file_candidates = [
                    os.path.join(task_dir, name)
                    for name in os.listdir(task_dir)
                    if os.path.isfile(os.path.join(task_dir, name))
                ]
                candidates = py_candidates or file_candidates
                if candidates:
                    script_path = max(candidates, key=lambda p: os.path.getmtime(p))
                    try:
                        with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                            code_text = f.read()
                        if len(code_text) > self.max_code_chars:
                            code_text = code_text[: self.max_code_chars] + "\n...truncated..."
                        code_block = f"\n\n```text\n{code_text}\n```"
                    except Exception as e:
                        code_block = f"\n\n[Code upload failed: {e}]"
                else:
                    code_block = "\n\n[No files found to upload]"
            result_payload = f"```bash\n{output}\n```\n*Workspace: {task_dir}*{code_block}"
            
            payload_str = json.dumps({
                "task_id": task_id,
                "provider_id": self.node_id,
                "result_payload": result_payload
            })
            headers = self.identity.get_auth_headers(payload_str)
            headers["Content-Type"] = "application/json"
            resp = await self._post_with_retry(f"{HUB_URL}/tasks/complete", payload_str=payload_str, headers=headers)
            if resp is None:
                print("[CLI Provider] Result submit failed")
                return
            print(f"[CLI Provider] Result submitted! Earned {bounty:.6f} SECONDS.\n")
        except Exception as e:
            print(f"[CLI Provider] Task failed: {e}")

if __name__ == "__main__":
    print("=" * 60)
    print("MEP Autonomous CLI Provider")
    print("WARNING: This node executes shell commands. Use sandboxing!")
    print("=" * 60)
    
    key_dir = os.getenv("MEP_KEY_DIR", os.path.join(os.path.expanduser("~"), ".mep"))
    os.makedirs(key_dir, exist_ok=True)
    key_path = os.getenv("MEP_CLI_KEY_PATH", os.path.join(key_dir, "mep_cli_provider.pem"))
    provider = MEPCLIProvider(key_path)
    print(f"[CLI Provider] Key path: {provider.identity.key_path}")
    if provider.identity.generated_new_key:
        print("[CLI Provider] Generated new key, node id will change")
    
    try:
        asyncio.run(provider.connect())
    except KeyboardInterrupt:
        provider.is_contributing = False
        print("\n[MEP] CLI Provider shut down.")
