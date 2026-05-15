import importlib.util
from pathlib import Path


_LOAD_RUNNER_PATH = Path(__file__).parent / "load" / "mep_load_runner.py"
_SPEC = importlib.util.spec_from_file_location("mep_load_runner", _LOAD_RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_LOAD_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_LOAD_RUNNER)
build_task_request = _LOAD_RUNNER.build_task_request


def test_load_runner_builds_spec_shaped_compute_task():
    body = build_task_request("node_load", "work item", 2.5)

    assert body["source"] == {"node_id": "node_load"}
    assert body["intent"] == {"type": "load.test.request"}
    assert body["task"] == {
        "instructions": "work item",
        "expected_output": {"result_type": "text"},
    }
    assert body["economics"] == {
        "bounty_ns": 2_500_000_000,
        "currency": "MEP_NS",
        "market": "compute",
        "payment_direction": "sender_to_receiver",
    }


def test_load_runner_builds_spec_shaped_data_task():
    body = build_task_request("node_load", "data item", -0.25)

    assert body["economics"] == {
        "bounty_ns": 250_000_000,
        "currency": "MEP_NS",
        "market": "data",
        "payment_direction": "receiver_to_sender",
    }
