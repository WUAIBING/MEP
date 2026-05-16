import asyncio
import json
import requests
import uuid
import time
import urllib.parse
import websockets
from reputation import ReputationManager
from identity import MEPIdentity
from ws_connect import ws_connect

WS_HEARTBEAT_INTERVAL_SECONDS = 60

class ChronosNode:
    """
    Simulated Clawdbot Client (Both Consumer & Provider)
    """
    def __init__(self, key_path: str, hub_url: str = "http://localhost:8000", ws_url: str = "ws://localhost:8000"):
        self.identity = MEPIdentity(key_path)
        self.node_id = self.identity.node_id
        self.hub_url = hub_url
        self.ws_url = ws_url
        self.reputation = ReputationManager(storage_path=f"reputation_{self.node_id}.json")
        self.is_sleeping = False
        
        # Track pending tasks we created (Consumer)
        self.my_pending_tasks: dict[str, dict] = {}

    def register(self):
        """Register to get 10 SECONDS."""
        print(f"[Node {self.node_id}] Registering with Hub...")
        resp = requests.post(f"{self.hub_url}/register", json={"pubkey": self.identity.pub_pem, "alias": "test"})
        data = resp.json()
        print(f"[Node {self.node_id}] Balance: {data['balance']}s")

    async def _handle_new_task(self, task_data: dict):
        """As a Provider (Sleeping), evaluate and execute the broadcasted task."""
        if not self.is_sleeping:
            return  # Awake nodes don't mine

        task_id = task_data["id"]
        payload = task_data["payload"]
        bounty = task_data["bounty"]
        print(f"[Node {self.node_id}] Broadcast received: Task {task_id[:6]} for {bounty}s")
        
        # 1. Check L2 Reputation of Consumer (Don't work for bad nodes)
        # (Mock implementation, skipping for brevity)
        
        # 2. Local execution using user's API keys (Mocked)
        await asyncio.sleep(1) # simulate think time
        result = f"Hello from {self.node_id}. I processed your payload: {payload[:20]}..."
        
        # 3. Submit proof of work
        payload_str = json.dumps({
            "task_id": task_id,
            "provider_id": self.node_id,
            "result_payload": result
        })
        headers = self.identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        resp = requests.post(f"{self.hub_url}/tasks/complete", data=payload_str, headers=headers)
        if resp.status_code == 200:
            print(f"[Node {self.node_id}] Mined {bounty}s! New Balance: {resp.json()['new_balance']}s")

    async def _handle_task_result(self, result_data: dict):
        """As a Consumer, receive the result and update Reputation."""
        task_id = result_data["task_id"]
        provider_id = result_data["provider_id"]
        result = result_data["result_payload"]
        
        print(f"\n[Node {self.node_id} (Consumer)] Result received for {task_id[:6]}")
        print(f" -> Provider: {provider_id}")
        print(f" -> Result: {result}")
        
        # L2 Reputation Evaluation
        score = self.reputation.evaluate_result(provider_id, result)
        print(f" -> Provider {provider_id} rated {score:.2f}/1.00 based on this result.\n")

    async def listen(self):
        """Persistent WebSocket connection."""
        import websockets

        ts = str(int(time.time()))
        sig = self.identity.sign(self.node_id, ts)
        sig_safe = urllib.parse.quote(sig)
        uri = f"{self.ws_url}/ws/{self.node_id}?timestamp={ts}&signature={sig_safe}"
        async with ws_connect(uri) as ws:
            print(f"[Node {self.node_id}] Connected to Hub via WebSocket.")
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    if data["event"] == "new_task":
                        asyncio.create_task(self._handle_new_task(data["data"]))
                    elif data["event"] == "task_result":
                        asyncio.create_task(self._handle_task_result(data["data"]))
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _heartbeat_loop(self, ws):
        while True:
            await asyncio.sleep(WS_HEARTBEAT_INTERVAL_SECONDS)
            await ws.send(json.dumps({"event": "heartbeat", "node_id": self.node_id, "ts": int(time.time())}))

    async def submit_task(self, payload: str, bounty: float, target_node: str | None = None, target_capability: str | None = None):
        """As a Consumer, create a task and lock SECONDS."""
        bounty_ns = int(abs(float(bounty)) * 1_000_000_000)
        if bounty < 0:
            market = "data"
            payment_direction = "receiver_to_sender"
        elif bounty == 0:
            market = "chat"
            payment_direction = "none"
        else:
            market = "compute"
            payment_direction = "sender_to_receiver"

        task_request = {
            "source": {"node_id": self.node_id},
            "intent": {"type": "analysis.request"},
            "task": {
                "instructions": payload,
                "expected_output": {"result_type": "text"},
            },
            "economics": {
                "bounty_ns": bounty_ns,
                "currency": "MEP_NS",
                "market": market,
                "payment_direction": payment_direction,
            },
        }
        if target_node or target_capability:
            task_request["routing"] = {}
            if target_node:
                task_request["routing"]["target_node_id"] = target_node
            if target_capability:
                task_request["routing"]["target_capability"] = target_capability

        payload_str = json.dumps(task_request)
        headers = self.identity.get_auth_headers(payload_str)
        headers["Content-Type"] = "application/json"
        resp = requests.post(f"{self.hub_url}/tasks/submit", data=payload_str, headers=headers)
        try:
            response_body = resp.json()
        except ValueError:
            response_body = {}
        task_id = response_body.get("task_id")
        if resp.status_code == 200 and response_body.get("status") == "success" and task_id:
            print(f"[Node {self.node_id} (Consumer)] Submitted Task {task_id[:6]} for {bounty}s")
        else:
            detail = response_body.get("detail") or resp.text
            print(f"Failed to submit task: {detail}")

async def run_demo():
    # Setup two nodes
    usa_node = ChronosNode(f"usa_node_{uuid.uuid4().hex[:6]}.pem")
    usa_node.is_sleeping = False
    usa_node.register()
    
    asia_node = ChronosNode(f"asia_node_{uuid.uuid4().hex[:6]}.pem")
    asia_node.is_sleeping = True # Asia goes to sleep and mines
    asia_node.register()

    # Start listening
    tasks = [
        asyncio.create_task(usa_node.listen()),
        asyncio.create_task(asia_node.listen())
    ]
    
    # Wait for connections
    await asyncio.sleep(1)

    # USA user creates a task
    await usa_node.submit_task("Write a long chapter about the Sleeping API", bounty=5.0)

    # Let the simulation run for a few seconds
    await asyncio.sleep(3)
    
    # Stop tasks
    for t in tasks:
        t.cancel()

if __name__ == "__main__":
    asyncio.run(run_demo())
