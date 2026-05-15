from typing import Optional


def build_task_envelope(
    node_id: str,
    instructions: str,
    bounty: float,
    *,
    intent_type: str = "analysis.request",
    target_node: Optional[str] = None,
    target_capability: Optional[str] = None,
    expected_output: Optional[dict] = None,
    payload_uri: Optional[str] = None,
    secret_data: Optional[str] = None,
) -> dict:
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

    body = {
        "source": {"node_id": node_id},
        "intent": {"type": intent_type},
        "task": {
            "instructions": instructions,
            "expected_output": expected_output or {"result_type": "text"},
        },
        "economics": {
            "bounty_ns": bounty_ns,
            "currency": "MEP_NS",
            "market": market,
            "payment_direction": payment_direction,
        },
    }
    if target_node or target_capability:
        body["routing"] = {}
        if target_node:
            body["routing"]["target_node_id"] = target_node
        if target_capability:
            body["routing"]["target_capability"] = target_capability
    if payload_uri is not None:
        body["payload_uri"] = payload_uri
    if secret_data is not None:
        body["secret_data"] = secret_data
    return body
