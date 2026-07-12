from node.task_envelope import build_task_envelope


def test_build_task_envelope_for_targeted_compute():
    body = build_task_envelope(
        "node_consumer",
        "pay for work",
        0.25,
        target_node="node_provider",
        target_capability="gemini-1.5-flash",
    )

    assert body["source"] == {"node_id": "node_consumer"}
    assert body["intent"] == {"type": "analysis.request"}
    assert body["task"] == {
        "instructions": "pay for work",
        "expected_output": {"result_type": "text"},
    }
    assert body["economics"] == {
        "bounty_ns": 250_000_000,
        "currency": "MEP_NS",
        "market": "compute",
        "payment_direction": "sender_to_receiver",
    }
    assert body["routing"] == {
        "target_node_id": "node_provider",
        "target_capability": "gemini-1.5-flash",
    }


def test_build_task_envelope_for_chat_and_data_markets():
    chat = build_task_envelope("node_consumer", "hello", 0.0, target_node="node_provider")
    data = build_task_envelope(
        "node_seller",
        "premium dataset",
        -0.5,
        target_node="node_buyer",
        secret_data="encrypted-data",
    )

    assert chat["economics"] == {
        "bounty_ns": 0,
        "currency": "MEP_NS",
        "market": "chat",
        "payment_direction": "none",
    }
    assert data["economics"] == {
        "bounty_ns": 500_000_000,
        "currency": "MEP_NS",
        "market": "data",
        "payment_direction": "receiver_to_sender",
    }
    assert data["secret_data"] == "encrypted-data"


def test_build_task_envelope_preserves_structured_task_metadata():
    body = build_task_envelope(
        "node_consumer",
        "Audit this repository.",
        0.0,
        intent_type="repo_audit.request",
        intent_priority="high",
        target_capability="repo_audit",
        expected_output={
            "result_type": "repo_audit_result",
            "format": "json",
            "artifact_allowed": True,
        },
        task_title="Repo audit: github.com/WUAIBING/MEP",
        task_inputs={
            "repo_audit": {
                "repo_url": "github.com/WUAIBING/MEP",
                "audit_type": "architecture_audit",
                "max_findings": 5,
            }
        },
    )

    assert body["intent"] == {"type": "repo_audit.request", "priority": "high"}
    assert body["task"] == {
        "instructions": "Audit this repository.",
        "expected_output": {
            "result_type": "repo_audit_result",
            "format": "json",
            "artifact_allowed": True,
        },
        "title": "Repo audit: github.com/WUAIBING/MEP",
        "inputs": {
            "repo_audit": {
                "repo_url": "github.com/WUAIBING/MEP",
                "audit_type": "architecture_audit",
                "max_findings": 5,
            }
        },
    }
    assert body["routing"] == {"target_capability": "repo_audit"}
