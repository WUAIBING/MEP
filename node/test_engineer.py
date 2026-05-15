
import os
import json
import requests
from identity import MEPIdentity
from task_envelope import build_task_envelope

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")
TARGET_NODE = "node_1c1a93dda148" # Myself

def send_engineer_task():
    key_path = os.path.expanduser("~/.mep/mep_ai_provider.pem")
    identity = MEPIdentity(key_path)
    
    payload = build_task_envelope(
        identity.node_id,
        "Write a python script to check my MEP balance using the local get_balance.py script and output just the balance number.",
        0.0,
        target_node=TARGET_NODE,
        target_capability="cli-agent",
    )
    
    payload_str = json.dumps(payload)
    headers = identity.get_auth_headers(payload_str)
    headers["Content-Type"] = "application/json"
    
    print(f"Sending Engineer Task to {TARGET_NODE}...")
    resp = requests.post(f"{HUB_URL}/tasks/submit", data=payload_str, headers=headers, timeout=10)
    print(resp.text)

if __name__ == "__main__":
    send_engineer_task()
