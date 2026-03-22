import requests
import os
from identity import MEPIdentity

HUB_URL = os.getenv("HUB_URL", "https://mep-hub.silentcopilot.ai")
KEY_PATH = os.path.join(os.path.expanduser("~"), ".mep", "mep_ai_provider.pem")

def check():
    if not os.path.exists(KEY_PATH):
        print("Key not found")
        return

    identity = MEPIdentity(KEY_PATH)
    print(f"Node: {identity.node_id}")
    
    headers = identity.get_auth_headers("")
    
    try:
        res = requests.get(f"{HUB_URL}/balance/{identity.node_id}", headers=headers)
        if res.status_code == 200:
            print(f"Response: {res.json()}")
        else:
            print(f"Error: {res.status_code} {res.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check()
