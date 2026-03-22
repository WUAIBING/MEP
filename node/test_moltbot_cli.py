
import os
import json
import requests
from identity import MEPIdentity

# Target: Moltbot's New CLI Node
TARGET_NODE = "node_925232892035"
HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")

def send_test_task():
    key_path = os.path.expanduser("~/.mep/mep_ai_provider.pem")
    identity = MEPIdentity(key_path)
    
    # Payload as requested by Moltbot
    payload = {
        "consumer_id": identity.node_id,
        "payload": "Write a Python function that calculates fibonacci numbers",
        "bounty": 0.5,
        "model_requirement": "cli-agent", # Trigger Claude Code
        "target_node": TARGET_NODE,
        "payload_uri": None,
        "secret_data": None
    }
    
    payload_str = json.dumps(payload)
    headers = identity.get_auth_headers(payload_str)
    headers["Content-Type"] = "application/json"
    
    try:
        print(f"Sending CLI test task to {TARGET_NODE}...")
        resp = requests.post(f"{HUB_URL}/tasks/submit", data=payload_str, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            print(f"Task sent! ID: {resp.json().get('task_id')}")
            print(f"Status: {resp.json().get('status')}")
        else:
            print(f"Failed: {resp.status_code} - {resp.text}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    send_test_task()
