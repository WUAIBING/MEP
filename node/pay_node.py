
import sys
import os
import json
import requests
from identity import MEPIdentity
from task_envelope import build_task_envelope

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")

def pay_node(target_node, amount=0.1):
    key_path = os.path.expanduser("~/.mep/mep_ai_provider.pem")
    identity = MEPIdentity(key_path)
    
    payload = build_task_envelope(
        identity.node_id,
        f"Payment test: {amount} SECONDS.",
        amount,
        target_node=target_node,
        target_capability="gemini-1.5-flash",
    )
    
    payload_str = json.dumps(payload)
    headers = identity.get_auth_headers(payload_str)
    headers["Content-Type"] = "application/json"
    
    print(f"Sending {amount} SECONDS to {target_node}...")
    try:
        resp = requests.post(f"{HUB_URL}/tasks/submit", data=payload_str, headers=headers, timeout=10)
        print(resp.text)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 pay_node.py <target_node_id> [amount]")
        sys.exit(1)
        
    target = sys.argv[1]
    amt = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    pay_node(target, amt)
