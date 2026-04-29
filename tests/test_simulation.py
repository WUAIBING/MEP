from core.ledger import get_ledger


def evaluate_task(is_sleeping: bool, task_payload: dict, rate_multiplier: float = 1.0) -> bool:
    if not is_sleeping:
        return False
    bounty = task_payload.get("bounty", 0)
    estimated_cost = len(task_payload.get("payload", "")) * 0.01 * rate_multiplier
    return bounty >= estimated_cost


def execute_task(is_sleeping: bool, payload: str) -> str:
    if not is_sleeping:
        raise PermissionError("Owner is awake. API locked.")
    return f"Processed payload '{payload[:10]}...' using local sleeping API."

def run_simulation():
    print("--- Starting Chronos Protocol Simulation ---")
    ledger = get_ledger()
    
    # 1. Setup Nodes
    active_node_id = "node_usa_active"
    sleeping_node_id = "node_asia_sleeping"
    
    ledger.register_node(active_node_id)
    ledger.register_node(sleeping_node_id)
    
    # Pre-fund active node for testing
    ledger.accounts[active_node_id] = 100.0 
    
    # 2. Asia goes to sleep
    asia_is_sleeping = True
    
    # 3. USA node creates a task
    print(f"\n[USA] Needs heavy code review. Balance: {ledger.get_balance(active_node_id)}s")
    task_payload = "def main(): pass # HUGE CODEBASE HERE"
    bounty = 10.0
    task_id = ledger.create_task(active_node_id, task_payload, bounty)
    
    # 4. Asia node evaluates and takes task
    mock_network_task = {"bounty": bounty, "payload": task_payload, "id": task_id}
    
    if evaluate_task(asia_is_sleeping, mock_network_task):
        print(f"\n[Asia] Evaluated task {task_id}. Bounty acceptable. Processing...")
        result = execute_task(asia_is_sleeping, task_payload)
        
        # Submit to L1 ledger
        ledger.submit_result(task_id, sleeping_node_id, result)
        
    # 5. Final Balances
    print("\n--- Final Ledger State ---")
    print(f"USA Node:  {ledger.get_balance(active_node_id)}s")
    print(f"Asia Node: {ledger.get_balance(sleeping_node_id)}s")

if __name__ == "__main__":
    run_simulation()
