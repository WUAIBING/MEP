"""
Hub API smoke tests — exercises the full task lifecycle using FastAPI TestClient.
No running server or Postgres needed; uses SQLite backend automatically.
"""
import base64
import json
import os
import sys
import time
import tempfile
import unittest

# Point hub DB at a temp file so tests don't pollute anything
_test_db = os.path.join(tempfile.gettempdir(), "mep_test_hub.db")
os.environ["MEP_SQLITE_PATH"] = _test_db
os.environ.setdefault("MEP_DATABASE_URL", "")  # force SQLite

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "hub"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

# Import hub app AFTER env vars are set — db import triggers init_db()
import db  # noqa: E402, F401
from main import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# Auth helpers (mirrors node/identity.py crypto)
# ---------------------------------------------------------------------------
def _make_identity():
    """Generate a keypair and return (private_key, pub_pem, node_id)."""
    private_key = Ed25519PrivateKey.generate()
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    from auth import derive_node_id
    node_id = derive_node_id(pub_pem)
    return private_key, pub_pem, node_id


def _auth_headers(private_key, node_id: str, payload_str: str) -> dict:
    """Build the X-MEP-* auth headers required by verify_request."""
    ts = str(int(time.time()))
    message = f"{payload_str}{ts}".encode("utf-8")
    signature = base64.b64encode(private_key.sign(message)).decode("utf-8")
    return {
        "X-MEP-NodeID": node_id,
        "X-MEP-Timestamp": ts,
        "X-MEP-Signature": signature,
        "Content-Type": "application/json",
    }


def _register(pub_pem: str) -> dict:
    resp = client.post("/register", json={"pubkey": pub_pem})
    assert resp.status_code == 200, f"Register failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestHealthEndpoint(unittest.TestCase):

    def test_health_returns_ok(self):
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("metrics", data)


class TestRegistration(unittest.TestCase):

    def test_register_new_node(self):
        _, pub_pem, _ = _make_identity()
        data = _register(pub_pem)
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["node_id"].startswith("node_"))
        self.assertGreater(data["balance"], 0)

    def test_duplicate_registration_preserves_balance(self):
        _, pub_pem, _ = _make_identity()
        data1 = _register(pub_pem)
        data2 = _register(pub_pem)
        self.assertEqual(data1["node_id"], data2["node_id"])
        self.assertEqual(data1["balance"], data2["balance"])


class TestBalance(unittest.TestCase):

    def test_get_balance(self):
        _, pub_pem, node_id = _make_identity()
        _register(pub_pem)
        resp = client.get(f"/balance/{node_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.json()["balance_seconds"], 0)

    def test_unknown_node_404(self):
        resp = client.get("/balance/node_doesnotexist")
        self.assertEqual(resp.status_code, 404)


class TestTaskLifecycle(unittest.TestCase):
    """Full happy-path: register consumer + provider, submit, bid, complete."""

    def test_submit_bid_complete(self):
        # Setup: two identities
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        provider_priv, provider_pub, provider_id = _make_identity()
        _register(consumer_pub)
        _register(provider_pub)

        # Submit task
        bounty = 1.0
        task_payload = json.dumps({
            "consumer_id": consumer_id,
            "payload": "What is 2+2?",
            "bounty": bounty,
        })
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Submit failed: {resp.text}")
        task_id = resp.json()["task_id"]

        # Bid on task
        bid_payload = json.dumps({
            "task_id": task_id,
            "provider_id": provider_id,
        })
        headers = _auth_headers(provider_priv, provider_id, bid_payload)
        resp = client.post("/tasks/bid", content=bid_payload, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "accepted")

        # Complete task
        result_payload = json.dumps({
            "task_id": task_id,
            "provider_id": provider_id,
            "result_payload": "4",
        })
        headers = _auth_headers(provider_priv, provider_id, result_payload)
        resp = client.post("/tasks/complete", content=result_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Complete failed: {resp.text}")
        self.assertEqual(resp.json()["status"], "success")
        self.assertEqual(resp.json()["earned"], bounty)

        # Verify provider balance increased
        resp = client.get(f"/balance/{provider_id}")
        self.assertGreater(resp.json()["balance_seconds"], 10.0)  # 10 starting + 1 earned

    def test_insufficient_balance_rejected(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)

        task_payload = json.dumps({
            "consumer_id": consumer_id,
            "payload": "Expensive task",
            "bounty": 99999.0,
        })
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Insufficient", resp.json()["detail"])


class TestAuthRejection(unittest.TestCase):

    def test_invalid_signature_rejected(self):
        priv, pub_pem, node_id = _make_identity()
        _register(pub_pem)

        payload = json.dumps({"consumer_id": node_id, "payload": "x", "bounty": 0.1})
        headers = {
            "X-MEP-NodeID": node_id,
            "X-MEP-Timestamp": str(int(time.time())),
            "X-MEP-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
            "Content-Type": "application/json",
        }
        resp = client.post("/tasks/submit", content=payload, headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_unregistered_node_rejected(self):
        priv, _, node_id = _make_identity()
        # Don't register
        payload = json.dumps({"consumer_id": node_id, "payload": "x", "bounty": 0.1})
        headers = _auth_headers(priv, node_id, payload)
        resp = client.post("/tasks/submit", content=payload, headers=headers)
        self.assertEqual(resp.status_code, 401)


def tearDownModule():
    """Clean up test database."""
    try:
        os.remove(_test_db)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
