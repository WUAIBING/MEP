"""
Hub API smoke tests — exercises the full task lifecycle using FastAPI TestClient.
No running server or Postgres needed; uses SQLite backend automatically.
"""
import base64
from contextlib import contextmanager
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
import main  # noqa: E402
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


def _diagnostic_headers(private_key, node_id: str) -> dict:
    """Build auth headers for GET /diagnostic Tier-2 auth."""
    ts = str(int(time.time()))
    message = f"{node_id}{ts}".encode("utf-8")
    signature = base64.b64encode(private_key.sign(message)).decode("utf-8")
    return {
        "X-MEP-NodeID": node_id,
        "X-MEP-Timestamp": ts,
        "X-MEP-Signature": signature,
    }


def _register(pub_pem: str) -> dict:
    resp = client.post("/register", json={"pubkey": pub_pem})
    assert resp.status_code == 200, f"Register failed: {resp.text}"
    return resp.json()


@contextmanager
def _interbot_validation(enabled: bool, legacy_policy: str = "dm_only"):
    old_enabled = main.INTERBOT_SPEC_VALIDATE_ENABLED
    old_policy = main.INTERBOT_LEGACY_POLICY
    main.INTERBOT_SPEC_VALIDATE_ENABLED = enabled
    main.INTERBOT_LEGACY_POLICY = legacy_policy
    try:
        yield
    finally:
        main.INTERBOT_SPEC_VALIDATE_ENABLED = old_enabled
        main.INTERBOT_LEGACY_POLICY = old_policy


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


class TestInterBotSpecValidation(unittest.TestCase):
    def test_rejects_invalid_structured_payload_when_enabled(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)
        invalid_payload = json.dumps(
            {
                "consumer_id": consumer_id,
                "payload": json.dumps({"spec_version": "mep.interbot.v1"}),
                "bounty": 0.0,
            }
        )
        headers = _auth_headers(consumer_priv, consumer_id, invalid_payload)
        with _interbot_validation(True):
            resp = client.post("/tasks/submit", content=invalid_payload, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Inter-bot payload", resp.json()["detail"])

    def test_accepts_valid_structured_dm_payload_when_enabled(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(consumer_pub)
        _register(target_pub)
        message = {
            "spec_version": "mep.interbot.v1",
            "message_id": "msg-123",
            "timestamp_ms": int(time.time() * 1000),
            "source": {"node_id": consumer_id, "alias": "consumer"},
            "target": {"node_id": target_id, "alias": "target"},
            "intent": {"type": "chat.request"},
            "task": {"instructions": "hello", "expected_output": {"result_type": "text"}},
            "economics": {"bounty_seconds": 0.0, "currency": "SECONDS"},
        }
        task_payload = json.dumps(
            {
                "consumer_id": consumer_id,
                "payload": json.dumps(message),
                "bounty": 0.0,
                "target_node": target_id,
            }
        )
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        with _interbot_validation(True):
            resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.json()["status"], ("success", "error"))

    def test_rejects_legacy_plaintext_non_dm_when_enabled(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)
        task_payload = json.dumps(
            {
                "consumer_id": consumer_id,
                "payload": "legacy plaintext non-dm",
                "bounty": 1.0,
            }
        )
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        with _interbot_validation(True):
            resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Legacy plaintext payload", resp.json()["detail"])


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


class TestDiagnosticEndpoint(unittest.TestCase):

    def test_public_diagnostic_for_registered_node(self):
        priv, pub_pem, node_id = _make_identity()
        _register(pub_pem)
        # Ensure node exists in registry table so public diagnostic can resolve it.
        update_payload = json.dumps({"alias": "diag-node"})
        headers = _auth_headers(priv, node_id, update_payload)
        update_resp = client.post("/registry/update", content=update_payload, headers=headers)
        self.assertEqual(update_resp.status_code, 200, f"Registry update failed: {update_resp.text}")

        resp = client.get(f"/diagnostic?node_id={node_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["node_id"], node_id)
        self.assertTrue(data["registered"])
        self.assertIn("availability", data)
        self.assertIn("last_heartbeat", data)

    def test_authenticated_diagnostic_rejects_invalid_signature(self):
        _, pub_pem, node_id = _make_identity()
        _register(pub_pem)
        bad_headers = {
            "X-MEP-NodeID": node_id,
            "X-MEP-Timestamp": str(int(time.time())),
            "X-MEP-Signature": "invalid-signature",
        }
        resp = client.get("/diagnostic", headers=bad_headers)
        self.assertEqual(resp.status_code, 401)

    def test_authenticated_diagnostic_succeeds_with_valid_signature(self):
        priv, pub_pem, node_id = _make_identity()
        _register(pub_pem)
        headers = _diagnostic_headers(priv, node_id)
        resp = client.get("/diagnostic", headers=headers)
        self.assertEqual(resp.status_code, 200, f"Diagnostic failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["node_id"], node_id)
        self.assertIn("ws_connected", data)
        self.assertIn("last_ws_activity", data)
        self.assertTrue(data["auth_ok"])


class TestMeshAssembly(unittest.TestCase):
    def setUp(self):
        conn = db._get_conn()
        conn.execute("UPDATE agent_registry SET availability = 'offline'")
        conn.commit()
        db._release_conn(conn)
        main.mesh_assemblies.clear()

    def _register_with_mesh_metadata(
        self,
        role: str,
        *,
        provider: str,
        thinking_mode: str,
        alias: str,
        availability: str = "online",
    ):
        priv, pub, node_id = _make_identity()
        _register(pub)
        update_payload = json.dumps(
            {
                "alias": alias,
                "availability": availability,
                "metadata": {
                    "ai_provider": provider,
                    "ai_status": "online",
                    "thinking_mode": thinking_mode,
                    "mesh_role_preference": role,
                },
            }
        )
        headers = _auth_headers(priv, node_id, update_payload)
        resp = client.post("/registry/update", content=update_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Registry update failed: {resp.text}")
        return priv, node_id

    def test_mesh_assemble_complete_with_four_roles(self):
        requester_priv, requester_id = self._register_with_mesh_metadata(
            "strategist",
            provider="deepseek",
            thinking_mode="reasoning",
            alias="Hermes",
        )
        self._register_with_mesh_metadata(
            "implementer",
            provider="yunwu",
            thinking_mode="code_reading",
            alias="Alisa",
        )
        self._register_with_mesh_metadata(
            "facilitator",
            provider="template",
            thinking_mode="aggregation",
            alias="Hub-Sentinel",
        )
        self._register_with_mesh_metadata(
            "scout",
            provider="echo",
            thinking_mode="ack_only",
            alias="Moltbot",
        )

        payload = json.dumps({"trigger": "brainstorm", "timeout_seconds": 180})
        headers = _auth_headers(requester_priv, requester_id, payload)
        resp = client.post("/mesh/assemble", content=payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Assemble failed: {resp.text}")
        data = resp.json()
        self.assertTrue(data["complete"])
        self.assertEqual(
            set(data["roles"].keys()),
            {"strategist", "implementer", "facilitator", "scout"},
        )

    def test_mesh_assemble_degraded_when_insufficient_nodes(self):
        requester_priv, requester_id = self._register_with_mesh_metadata(
            "strategist",
            provider="deepseek",
            thinking_mode="reasoning",
            alias="Solo",
        )
        payload = json.dumps({"trigger": "incident"})
        headers = _auth_headers(requester_priv, requester_id, payload)
        resp = client.post("/mesh/assemble", content=payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Assemble failed: {resp.text}")
        data = resp.json()
        self.assertFalse(data["complete"])
        self.assertIn("degraded_warning", data)
        self.assertIn("strategist", data["roles"])

    def test_mesh_status_reports_drop_and_reassignment(self):
        requester_priv, requester_id = self._register_with_mesh_metadata(
            "strategist",
            provider="deepseek",
            thinking_mode="reasoning",
            alias="Hermes",
        )
        self._register_with_mesh_metadata(
            "implementer",
            provider="yunwu",
            thinking_mode="code_reading",
            alias="Alisa",
        )
        self._register_with_mesh_metadata(
            "facilitator",
            provider="template",
            thinking_mode="aggregation",
            alias="Hub-Sentinel",
        )
        self._register_with_mesh_metadata(
            "scout",
            provider="echo",
            thinking_mode="ack_only",
            alias="Moltbot",
        )
        self._register_with_mesh_metadata(
            "scout",
            provider="echo",
            thinking_mode="ack_only",
            alias="Backup-Scout",
        )

        assemble_payload = json.dumps({"trigger": "planning"})
        assemble_headers = _auth_headers(requester_priv, requester_id, assemble_payload)
        assemble_resp = client.post("/mesh/assemble", content=assemble_payload, headers=assemble_headers)
        self.assertEqual(assemble_resp.status_code, 200, f"Assemble failed: {assemble_resp.text}")
        assembly = assemble_resp.json()
        scout_node_id = assembly["roles"]["scout"]["node_id"]
        db.update_registry_availability(scout_node_id, "offline", time.time())

        status_headers = _auth_headers(requester_priv, requester_id, "")
        status_resp = client.get(
            f"/mesh/status?assembly_id={assembly['assembly_id']}",
            headers=status_headers,
        )
        self.assertEqual(status_resp.status_code, 200, f"Status failed: {status_resp.text}")
        status_data = status_resp.json()
        self.assertFalse(status_data["complete"])
        self.assertIn("scout", status_data["dropped_roles"])
        self.assertIn("scout", status_data["reassignment_suggestions"])
        self.assertNotEqual(
            status_data["reassignment_suggestions"]["scout"]["node_id"],
            scout_node_id,
        )


class TestBrainstormSessions(unittest.TestCase):
    def setUp(self):
        main.brainstorm_sessions.clear()

    def test_create_post_and_read_session(self):
        owner_priv, owner_pub, owner_id = _make_identity()
        p1_priv, p1_pub, p1_id = _make_identity()
        p2_priv, p2_pub, p2_id = _make_identity()
        _register(owner_pub)
        _register(p1_pub)
        _register(p2_pub)

        create_payload = json.dumps(
            {
                "owner_id": owner_id,
                "participants": [p1_id, p2_id],
                "topic": "Loop-free architecture",
            }
        )
        create_headers = _auth_headers(owner_priv, owner_id, create_payload)
        create_resp = client.post("/brainstorm/sessions/create", content=create_payload, headers=create_headers)
        self.assertEqual(create_resp.status_code, 200, f"Create failed: {create_resp.text}")
        session_id = create_resp.json()["session_id"]

        post_payload = json.dumps(
            {
                "session_id": session_id,
                "message": "I suggest we add a shared session timeline.",
            }
        )
        post_headers = _auth_headers(p1_priv, p1_id, post_payload)
        post_resp = client.post("/brainstorm/sessions/post", content=post_payload, headers=post_headers)
        self.assertEqual(post_resp.status_code, 200, f"Post failed: {post_resp.text}")

        get_headers = _auth_headers(p2_priv, p2_id, "")
        get_resp = client.get(f"/brainstorm/sessions/{session_id}", headers=get_headers)
        self.assertEqual(get_resp.status_code, 200, f"Get failed: {get_resp.text}")
        data = get_resp.json()
        self.assertEqual(data["topic"], "Loop-free architecture")
        self.assertEqual(data["message_count"], 1)
        self.assertEqual(data["messages"][0]["sender_id"], p1_id)

    def test_non_participant_cannot_post(self):
        owner_priv, owner_pub, owner_id = _make_identity()
        p1_priv, p1_pub, p1_id = _make_identity()
        outsider_priv, outsider_pub, outsider_id = _make_identity()
        _register(owner_pub)
        _register(p1_pub)
        _register(outsider_pub)

        create_payload = json.dumps({"owner_id": owner_id, "participants": [p1_id]})
        create_headers = _auth_headers(owner_priv, owner_id, create_payload)
        create_resp = client.post("/brainstorm/sessions/create", content=create_payload, headers=create_headers)
        self.assertEqual(create_resp.status_code, 200)
        session_id = create_resp.json()["session_id"]

        post_payload = json.dumps({"session_id": session_id, "message": "Intruding message"})
        post_headers = _auth_headers(outsider_priv, outsider_id, post_payload)
        post_resp = client.post("/brainstorm/sessions/post", content=post_payload, headers=post_headers)
        self.assertEqual(post_resp.status_code, 403)


def tearDownModule():
    """Clean up test database."""
    try:
        os.remove(_test_db)
    except OSError:
        pass

class TestBiddingTimeout(unittest.TestCase):
    """Test bidding task timeout + refund flow"""

    def test_stale_bidding_task_expired_and_refunded(self):
        """Verify stale bidding tasks are expired and bounty refunded to consumer"""
        # Setup: consumer and provider
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        provider_priv, provider_pub, provider_id = _make_identity()
        _register(consumer_pub)
        _register(provider_pub)

        # Get initial balance
        resp = client.get(f"/balance/{consumer_id}")
        initial_balance = resp.json()["balance_seconds"]

        # Submit task with bounty (escrow created)
        bounty = 1.0
        task_payload = json.dumps({
            "consumer_id": consumer_id,
            "payload": "Stale task that will timeout",
            "bounty": bounty,
        })
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Submit failed: {resp.text}")
        task_id = resp.json()["task_id"]

        # Verify balance is escrowed (reduced by bounty)
        resp = client.get(f"/balance/{consumer_id}")
        self.assertLess(resp.json()["balance_seconds"], initial_balance,
                       "Consumer balance should decrease by bounty")

        # Simulate task aging: directly update task's updated_at in DB to be old
        # (In real system, timeout worker does this after ASSIGNMENT_TIMEOUT_SECONDS)
        import time as t
        old_time = t.time() - 7200  # 2 hours ago (well beyond 1 hour timeout)
        conn = db._get_conn()
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
            (old_time, task_id)
        )
        conn.commit()
        db._release_conn(conn)

        # Run the bidding timeout sweep
        import asyncio
        from main import _sweep_bidding_timeouts
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_sweep_bidding_timeouts())
        finally:
            loop.close()

        # Verify task status from DB helper (not /tasks/{task_id}, which does not exist)
        task = db.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "expired", "Task should be expired after timeout")

        # Verify consumer balance is restored (refunded)
        resp = client.get(f"/balance/{consumer_id}")
        self.assertEqual(resp.json()["balance_seconds"], initial_balance,
                        "Consumer balance should be restored after refund")


class TestConsumerDefinedTimeout(unittest.TestCase):
    """Per-task consumer timeout should override global default within bounds."""

    def test_consumer_timeout_expires_task_earlier(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)

        resp = client.get(f"/balance/{consumer_id}")
        initial_balance = resp.json()["balance_seconds"]

        bounty = 1.0
        task_payload = json.dumps({
            "consumer_id": consumer_id,
            "payload": "Expire me quickly",
            "bounty": bounty,
            "expires_in_seconds": 60,
        })
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Submit failed: {resp.text}")
        task_id = resp.json()["task_id"]

        # This is older than the consumer timeout (60s) but younger than global default (3600s).
        old_time = time.time() - 120
        conn = db._get_conn()
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
            (old_time, task_id)
        )
        conn.commit()
        db._release_conn(conn)

        import asyncio
        from main import _sweep_bidding_timeouts
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_sweep_bidding_timeouts())
        finally:
            loop.close()

        task = db.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "expired")

        resp = client.get(f"/balance/{consumer_id}")
        self.assertEqual(resp.json()["balance_seconds"], initial_balance)


if __name__ == "__main__":
    unittest.main()
