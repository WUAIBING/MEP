
import sys
import os
import json
import requests
from identity import MEPIdentity
from task_envelope import build_task_envelope

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")

def buy_data(target_node):
    key_path = os.path.expanduser("~/.mep/mep_ai_provider.pem")
    identity = MEPIdentity(key_path)
    
    payload = build_task_envelope(
        identity.node_id,
        "I want to buy the secret dataset.",
        0.5,
        target_node=target_node,
        target_capability="data-purchase",
    )
    
    payload_str = json.dumps(payload)
    headers = identity.get_auth_headers(payload_str)
    headers["Content-Type"] = "application/json"
    
    print(f"Buying Data from {target_node}...")
    try:
        resp = requests.post(f"{HUB_URL}/tasks/submit", data=payload_str, headers=headers, timeout=10)
        print(resp.text)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 buy_data.py <target_node_id>")
        sys.exit(1)
    
    target = sys.argv[1]
    buy_data(target)
