import base64
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient


DB_PATH = os.path.join(tempfile.gettempdir(), f"mep_test_{uuid.uuid4().hex}.db")
os.environ["MEP_SQLITE_PATH"] = DB_PATH
os.environ["MEP_DATABASE_URL"] = ""
os.environ["MEP_REQUIRE_TLS"] = "false"
os.environ["MEP_ADMIN_KEY"] = "test-admin-key"
os.environ["MEP_FEDERATION_ENABLED"] = "true"

HUB_DIR = Path(__file__).resolve().parents[1] / "hub"
sys.path.insert(0, str(HUB_DIR))

import auth  # noqa: E402
from main import app  # noqa: E402


client = TestClient(app)


def _make_identity() -> tuple[Ed25519PrivateKey, str, str]:
    private_key = Ed25519PrivateKey.generate()
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    # Hub registration normalizes PEM with .strip(), so we derive from the same normalized value.
    node_id = auth.derive_node_id(pub_pem.strip())
    return private_key, pub_pem, node_id


def _auth_headers(private_key: Ed25519PrivateKey, node_id: str, payload_str: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = f"{payload_str}{timestamp}".encode("utf-8")
    signature = base64.b64encode(private_key.sign(message)).decode("utf-8")
    return {
        "X-MEP-NodeID": node_id,
        "X-MEP-Timestamp": timestamp,
        "X-MEP-Signature": signature,
        "Content-Type": "application/json",
    }


def _register(pub_pem: str) -> dict:
    response = client.post("/register", json={"pubkey": pub_pem})
    assert response.status_code == 200
    return response.json()


def _submit_task(
    consumer_priv: Ed25519PrivateKey,
    consumer_id: str,
    *,
    payload: str,
    bounty: float,
    idem_key: str | None = None,
) -> dict:
    submit_payload = json.dumps(
        {
            "consumer_id": consumer_id,
            "payload": payload,
            "bounty": bounty,
        }
    )
    submit_headers = _auth_headers(consumer_priv, consumer_id, submit_payload)
    if idem_key:
        submit_headers["X-MEP-Idempotency-Key"] = idem_key
    submit_response = client.post("/tasks/submit", content=submit_payload, headers=submit_headers)
    assert submit_response.status_code == 200
    return submit_response.json()


def _bid_task(provider_priv: Ed25519PrivateKey, provider_id: str, task_id: str) -> dict:
    bid_payload = json.dumps({"task_id": task_id, "provider_id": provider_id})
    bid_headers = _auth_headers(provider_priv, provider_id, bid_payload)
    bid_response = client.post("/tasks/bid", content=bid_payload, headers=bid_headers)
    assert bid_response.status_code == 200
    return bid_response.json()


def _complete_task(
    provider_priv: Ed25519PrivateKey,
    provider_id: str,
    task_id: str,
    *,
    result_payload: str = "done",
    idem_key: str | None = None,
) -> dict:
    complete_payload = json.dumps(
        {
            "task_id": task_id,
            "provider_id": provider_id,
            "result_payload": result_payload,
        }
    )
    complete_headers = _auth_headers(provider_priv, provider_id, complete_payload)
    if idem_key:
        complete_headers["X-MEP-Idempotency-Key"] = idem_key
    complete_response = client.post("/tasks/complete", content=complete_payload, headers=complete_headers)
    assert complete_response.status_code == 200
    return complete_response.json()


def _open_dispute(consumer_priv: Ed25519PrivateKey, consumer_id: str, task_id: str, reason: str) -> dict:
    dispute_payload = json.dumps({"task_id": task_id, "reason": reason})
    dispute_headers = _auth_headers(consumer_priv, consumer_id, dispute_payload)
    dispute_response = client.post("/disputes/open", content=dispute_payload, headers=dispute_headers)
    return {"status_code": dispute_response.status_code, "json": dispute_response.json()}


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert "metrics" in data


def test_register_and_balance_flow() -> None:
    _, pub_pem, node_id = _make_identity()
    payload = _register(pub_pem)
    assert payload["status"] == "success"
    assert payload["node_id"] == node_id
    assert payload["balance"] >= 10.0

    response = client.get(f"/balance/{node_id}")
    assert response.status_code == 200
    assert response.json()["balance_seconds"] >= 10.0


def test_task_submit_bid_complete_happy_path() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    provider_priv, provider_pub, provider_id = _make_identity()
    _register(consumer_pub)
    _register(provider_pub)

    submit_data = _submit_task(consumer_priv, consumer_id, payload="compute this", bounty=1.5)
    task_id = submit_data["task_id"]
    bid_data = _bid_task(provider_priv, provider_id, task_id)
    assert bid_data["status"] == "accepted"
    complete_data = _complete_task(provider_priv, provider_id, task_id, result_payload="done")
    assert complete_data["status"] == "success"
    assert complete_data["earned"] == 1.5


def test_submit_rejects_missing_payload_and_uri() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    _register(consumer_pub)

    payload = json.dumps(
        {
            "consumer_id": consumer_id,
            "payload": "",
            "payload_uri": "",
            "bounty": 0.5,
        }
    )
    headers = _auth_headers(consumer_priv, consumer_id, payload)
    response = client.post("/tasks/submit", content=payload, headers=headers)
    assert response.status_code == 400
    assert "payload or payload_uri" in response.json()["detail"]


def test_submit_idempotency_returns_same_task_id() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    _register(consumer_pub)
    idem_key = f"idem-submit-{uuid.uuid4().hex}"

    first = _submit_task(consumer_priv, consumer_id, payload="same request", bounty=0.2, idem_key=idem_key)
    second = _submit_task(consumer_priv, consumer_id, payload="same request", bounty=0.2, idem_key=idem_key)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert first["task_id"] == second["task_id"]


def test_cancel_idempotency_is_stable() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    _register(consumer_pub)
    task_id = _submit_task(consumer_priv, consumer_id, payload="cancel me", bounty=0.3)["task_id"]
    idem_key = f"idem-cancel-{uuid.uuid4().hex}"

    cancel_payload = json.dumps({"task_id": task_id})
    cancel_headers = _auth_headers(consumer_priv, consumer_id, cancel_payload)
    cancel_headers["X-MEP-Idempotency-Key"] = idem_key
    first = client.post("/tasks/cancel", content=cancel_payload, headers=cancel_headers)
    second = client.post("/tasks/cancel", content=cancel_payload, headers=cancel_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["state"] == "cancelled"


def test_complete_idempotency_is_stable() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    provider_priv, provider_pub, provider_id = _make_identity()
    _register(consumer_pub)
    _register(provider_pub)
    task_id = _submit_task(consumer_priv, consumer_id, payload="complete once", bounty=0.6)["task_id"]
    assert _bid_task(provider_priv, provider_id, task_id)["status"] == "accepted"
    idem_key = f"idem-complete-{uuid.uuid4().hex}"

    first = _complete_task(provider_priv, provider_id, task_id, result_payload="ok", idem_key=idem_key)
    second = _complete_task(provider_priv, provider_id, task_id, result_payload="ok", idem_key=idem_key)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert first == second


def test_open_dispute_and_resolve_consumer_chargeback() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    provider_priv, provider_pub, provider_id = _make_identity()
    _register(consumer_pub)
    _register(provider_pub)
    task_id = _submit_task(consumer_priv, consumer_id, payload="disputable work", bounty=1.2)["task_id"]
    assert _bid_task(provider_priv, provider_id, task_id)["status"] == "accepted"
    assert _complete_task(provider_priv, provider_id, task_id, result_payload="bad result")["status"] == "success"

    opened = _open_dispute(consumer_priv, consumer_id, task_id, "Result quality is not acceptable.")
    assert opened["status_code"] == 200
    assert opened["json"]["status"] == "success"

    resolve_payload = {"task_id": task_id, "resolution": "consumer"}
    resolve_headers = {"X-MEP-Admin-Key": "test-admin-key"}
    resolved = client.post("/disputes/resolve", json=resolve_payload, headers=resolve_headers)
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "success"
    assert body["resolution"] == "consumer"
    assert body["escrow_status"] == "chargeback"


def test_open_dispute_rejects_non_positive_bounty_task() -> None:
    consumer_priv, consumer_pub, consumer_id = _make_identity()
    provider_priv, provider_pub, provider_id = _make_identity()
    _register(consumer_pub)
    _register(provider_pub)
    task_id = _submit_task(consumer_priv, consumer_id, payload="free chat", bounty=0.0)["task_id"]
    assert _bid_task(provider_priv, provider_id, task_id)["status"] == "accepted"
    assert _complete_task(provider_priv, provider_id, task_id, result_payload="hi")["status"] == "success"

    opened = _open_dispute(consumer_priv, consumer_id, task_id, "This should not allow dispute.")
    assert opened["status_code"] == 400
    assert "positive bounty" in opened["json"]["detail"]


def test_federation_peer_admin_key_required() -> None:
    response = client.post("/federation/peers", json={"hub_url": "https://peer.example.com"})
    assert response.status_code == 403


def test_federation_discovery_includes_local_registry_result() -> None:
    node_priv, node_pub, node_id = _make_identity()
    _register(node_pub)

    update_payload = json.dumps(
        {
            "alias": "federation-node",
            "skills": ["chat", "analysis"],
            "models": ["gpt-test-model"],
            "availability": "online",
        }
    )
    update_headers = _auth_headers(node_priv, node_id, update_payload)
    updated = client.post("/registry/update", content=update_payload, headers=update_headers)
    assert updated.status_code == 200

    discovery = client.get("/federation/discovery", params={"model": "gpt-test-model", "include_local": True})
    assert discovery.status_code == 200
    payload = discovery.json()
    assert payload["status"] == "success"
    assert payload["count"] >= 1
    assert any(item.get("node_id") == node_id for item in payload["results"])


def teardown_module() -> None:
    try:
        os.remove(DB_PATH)
    except OSError:
        pass
