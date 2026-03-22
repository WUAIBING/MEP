
#!/usr/bin/env python3
"""
MEP AI Provider - For AI agents like Hub Sentinel (Gemini 3.1 Pro).
Includes X25519 Encryption, Data Market Logic, and Autonomous Engineer Routing.
"""
import asyncio
import os
import sys
import json
import subprocess
import tempfile
import websockets
import requests
import time
import urllib.parse
import boto3
from botocore.config import Config
from dotenv import load_dotenv
from identity import MEPIdentity

# Load env for R2 keys
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")
WS_URL = os.getenv("WS_URL", "wss://mep-hub.silentcopilot.ai")

class R2Storage:
    def __init__(self):
        self.endpoint = os.getenv("R2_ENDPOINT")
        self.access_key = os.getenv("R2_ACCESS_KEY_ID")
        self.secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        self.bucket = os.getenv("R2_BUCKET_NAME", "mep-data")
        
        if self.endpoint and self.access_key:
            self.client = boto3.client(
                's3',
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                config=Config(signature_version='s3v4')
            )
        else:
            self.client = None

    def generate_presigned_url(self, object_name, expiration=3600):
        if not self.client:
            return None
        try:
            url = self.client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket, 'Key': object_name},
                ExpiresIn=expiration
            )
            return url
        except Exception as e:
            print(f"[R2] Error generating URL: {e}")
            return None

class MEPAIProvider:
    def __init__(self, key_path: str):
        self.identity = MEPIdentity(key_path)
        self.r2 = R2Storage()
        self.node_id = self.identity.node_id
        # ... rest of init ...
        self.balance = 0.0
        self.is_mining = True
        self.workspace_dir = os.path.join(tempfile.gettempdir(), "mep_ai_workspaces")
        os.makedirs(self.workspace_dir, exist_ok=True)
        
        # AI API configuration
        self.ai_api_cmd = os.getenv("MEP_AI_AGENT_CMD", sys.executable + " " + os.path.join(os.path.dirname(__file__), "mep_ai_agent.py"))
        
    async def connect(self):
        """Connect to MEP Hub and start mining."""
        print(f"[AI Provider {self.node_id}] Starting...")
        
        # Register with hub (Send Encryption Key)
        try:
            resp = requests.post(
                f"{HUB_URL}/register",
                json={
                    "pubkey": self.identity.pub_pem,
                    "capabilities": ["ai-agent", "data-host"],
                    "x25519_public_key": self.identity.x25519_public_key
                },
                headers=self.identity.get_auth_headers(""),
                timeout=10
            )
            data = resp.json()
            self.balance = data.get("balance", 0.0)
            print(f"[AI Provider {self.node_id}] Registered. Balance: {self.balance:.6f} SECONDS")
        except Exception as e:
            print(f"[AI Provider {self.node_id}] Registration failed: {e}")
            return
        
        # WebSocket connection loop
        while self.is_mining:
            ts = str(int(time.time()))
            sig = self.identity.sign(self.node_id, ts)
            safe_sig = urllib.parse.quote(sig)
            base_ws = WS_URL.replace('https://', 'wss://').replace('http://', 'ws://')
            uri = f"{base_ws}/ws/{self.node_id}?timestamp={ts}&signature={safe_sig}"
            
            try:
                print(f"[AI Provider {self.node_id}] Connecting to WebSocket...")
                async with websockets.connect(uri) as ws:
                    print(f"[AI Provider {self.node_id}] Connected. Awaiting AI tasks...")
                    
                    while self.is_mining:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
                            data = json.loads(msg)
                            
                            if data["event"] == "new_task":
                                await self.process_task(data["data"])
                            elif data["event"] == "rfc":
                                await self.handle_rfc(data["data"])
                                
                        except asyncio.TimeoutError:
                            try:
                                await ws.ping()
                            except Exception:
                                break
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            print("[AI Provider] Connection closed")
                            break
            except Exception as e:
                print(f"[AI Provider] WebSocket error: {e}")
                
            if self.is_mining:
                print("[AI Provider] Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
    
    async def handle_rfc(self, rfc_data: dict):
        """Evaluate Request For Compute and submit Bid."""
        task_id = rfc_data["id"]
        bounty = rfc_data["bounty"]
        
        # Safety: Don't buy expensive data unless allowed
        # Negative bounty = data market (provider pays)
        if bounty < 0:
            max_purchase_price = float(os.getenv("MEP_MAX_PURCHASE_PRICE", "0.0"))
            cost = abs(bounty)
            if cost > max_purchase_price:
                print(f"[AI Provider] Ignored RFC {task_id[:8]} (cost {cost:.6f} exceeds max {max_purchase_price:.6f})")
                return
            
        print(f"[AI Provider] Received RFC {task_id[:8]} for {bounty:.6f} SECONDS. Placing bid...")
        
        try:
            payload_str = json.dumps({
                "task_id": task_id,
                "provider_id": self.node_id
            })
            headers = self.identity.get_auth_headers(payload_str)
            headers["Content-Type"] = "application/json"
            resp = requests.post(f"{HUB_URL}/tasks/bid", data=payload_str, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                bid_data = resp.json()
                print("[AI Provider] Bid accepted! Task assigned.")
                # Process task
                _secret_data = bid_data.get("secret_data")
                # We need to manually inject the consumer key if it was passed in RFC or Task?
                # Usually new_task event has it.
                # Here we wait for new_task event or process immediately? 
                # The logic flow is: Bid -> Success -> (Hub sends 'new_task' event to Provider).
                # So we don't call process_task here usually, but the original code did?
                # Actually, Hub usually sends 'new_task' via WS after bid.
                # So we can just return and let the WS loop handle it.
                pass 
            else:
                print(f"[AI Provider] Bid failed: {resp.text}")
        except Exception as e:
            print(f"[AI Provider] Bid error: {e}")
    
    async def process_task(self, task_data: dict):
        """Execute the task using AI API."""
        task_id = task_data["id"]
        payload = task_data["payload"]
        _payload_uri = task_data.get("payload_uri")
        bounty = task_data["bounty"]
        model_req = task_data.get("model_requirement", "")
        consumer_pubkey_b64 = task_data.get("consumer_x25519_pubkey")
        secret_data = task_data.get("secret_data") # If I am the Buyer
        
        print(f"[AI Provider] Processing task {task_id[:8]} for {bounty:.6f} SECONDS")
        
        # --- Logic as Buyer (Consumer) ---
        if secret_data:
            print("[AI Provider] 💾 Received SECRET data.")
            if consumer_pubkey_b64: # Actually, this would be provider's key if I am consumer? 
                # No, if I am consumer, I decrypt with MY private key.
                # But here I am running as Provider.
                # Wait, if I am the Provider, I shouldn't receive secret_data unless I am sub-contracting?
                # Ah, 'secret_data' in task_data usually means the Consumer sent it to me?
                # No, in Data Market: Seller (Provider) sends secret to Buyer (Consumer).
                # So here, if I am the Provider, I am SELLING. I don't receive secret_data.
                # UNLESS the Consumer sent me encrypted inputs?
                pass

        # --- Logic as Seller (Provider) ---
        ai_response = ""
        
        if model_req == "data-purchase":
            # I am selling data.
            print("[AI Provider] 📦 Data Purchase Request. Generating R2 Link...")
            
            # Generate Presigned URL for a sample file
            r2_url = self.r2.generate_presigned_url("dataset_v1.zip")
            
            if r2_url:
                payload_content = f"DOWNLOAD_LINK: {r2_url}"
            else:
                payload_content = "DATA_PACKET_R2_LINK_XYZ_123" # Fallback
            
            if consumer_pubkey_b64:
                try:
                    encrypted_data = self.identity.encrypt_for_peer(consumer_pubkey_b64, payload_content)
                    ai_response = encrypted_data
                    print("[AI Provider] ✅ R2 Link Encrypted for Consumer.")
                except Exception as e:
                    print(f"[AI Provider] Encryption failed: {e}")
                    ai_response = "ERROR_ENCRYPTION_FAILED"
            else:
                print("[AI Provider] ❌ No Consumer Public Key provided by Hub!")
                ai_response = "ERROR_NO_PUBKEY"
        
        else:
            # Normal AI Execution
            try:
                cmd = self.ai_api_cmd.split()
                if model_req == "cli-agent" or "engineer" in model_req:
                    print("[AI Provider] 🚀 Routing to Sentinel Engineer...")
                    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "sentinel_engineer_v2.py")]
                
                result = subprocess.run(
                    cmd,
                    input=payload,
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                
                if result.returncode == 0:
                    ai_response = result.stdout.strip()
                    print("[AI Provider] ✅ AI response generated")
                else:
                    ai_response = f"AI API error: {result.stderr}"
                    print(f"[AI Provider] ❌ AI API failed: {result.stderr}")
            except Exception as e:
                ai_response = f"Processing error: {e}"
                print(ai_response)

        # Submit result
        # If I sold data, 'ai_response' IS the secret data (encrypted).
        # We submit it as 'result_payload' (if plaintext/public) OR 'secret_data' (if encrypted/private)?
        # The Hub API usually has a field for the result.
        # Let's assume 'result_payload' is used for the delivery.
        
        result_payload = {
            "task_id": task_id,
            "provider_id": self.node_id,
            "result_payload": ai_response 
        }
        
        result_str = json.dumps(result_payload)
        headers = self.identity.get_auth_headers(result_str)
        headers["Content-Type"] = "application/json"
        
        try:
            resp = requests.post(f"{HUB_URL}/tasks/complete", data=result_str, headers=headers, timeout=20)
            
            if resp.status_code == 200:
                data = resp.json()
                self.balance = data["new_balance"]
                print(f"[AI Provider] Earned {bounty:.6f} SECONDS!")
                print(f"  New balance: {self.balance:.6f} SECONDS")
                time.sleep(2.0)
            else:
                print(f"[AI Provider] Failed to submit: {resp.text}")
        except Exception as e:
            print(f"[AI Provider] Submit error: {e}")
    
    def stop(self):
        self.is_mining = False
        print(f"[AI Provider {self.node_id}] Stopping...")

async def main():
    key_dir = os.getenv("MEP_KEY_DIR", os.path.join(os.path.expanduser("~"), ".mep"))
    os.makedirs(key_dir, exist_ok=True)
    key_path = os.getenv("MEP_PROVIDER_KEY_PATH", os.path.join(key_dir, "mep_ai_provider.pem"))
    
    provider = MEPAIProvider(key_path)
    try:
        await provider.connect()
    except KeyboardInterrupt:
        provider.stop()

if __name__ == "__main__":
    asyncio.run(main())
