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


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


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

    def test_register_persists_x25519_public_key_in_registry(self):
        _, pub_pem, node_id = _make_identity()
        resp = client.post("/register", json={"pubkey": pub_pem, "alias": "enc-node", "x25519_public_key": "encpub"})
        self.assertEqual(resp.status_code, 200, f"Register failed: {resp.text}")

        registry = db.get_registry(node_id)
        self.assertIsNotNone(registry)
        self.assertEqual(registry["alias"], "enc-node")
        self.assertEqual(registry["x25519_public_key"], "encpub")


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


class TestThreeMarketSpecConformance(unittest.TestCase):
    """Spec-shaped task envelopes should support compute, chat/DM, and data markets."""

    def setUp(self):
        # The /register rate-limit bucket is keyed only by client IP, so it is
        # shared across the whole suite. Reset it for deterministic isolation.
        main.rate_limits.clear()

    def _submit_spec_task(
        self,
        private_key,
        node_id: str,
        *,
        instructions: str,
        bounty_ns: int,
        market: str,
        payment_direction: str,
        target_node: str | None = None,
        target_capability: str | None = None,
        secret_data: str | None = None,
    ) -> str:
        payload = {
            "source": {"node_id": node_id},
            "intent": {"type": "conformance.request"},
            "task": {
                "instructions": instructions,
                "expected_output": {"result_type": "text"},
            },
            "economics": {
                "bounty_ns": bounty_ns,
                "currency": "MEP_NS",
                "market": market,
                "payment_direction": payment_direction,
            },
        }
        routing = {}
        if target_node:
            routing["target_node_id"] = target_node
        if target_capability:
            routing["target_capability"] = target_capability
        if routing:
            payload["routing"] = routing
        if secret_data is not None:
            payload["secret_data"] = secret_data

        task_payload = json.dumps(payload)
        headers = _auth_headers(private_key, node_id, task_payload)
        resp = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Submit failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["status"], "success")
        return data["task_id"]

    def _bid(self, private_key, provider_id: str, task_id: str) -> dict:
        bid_payload = json.dumps({"task_id": task_id, "provider_id": provider_id})
        headers = _auth_headers(private_key, provider_id, bid_payload)
        resp = client.post("/tasks/bid", content=bid_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Bid failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["status"], "accepted")
        return data

    def _complete(self, private_key, provider_id: str, task_id: str, result_payload: str) -> dict:
        complete_payload = json.dumps(
            {
                "task_id": task_id,
                "provider_id": provider_id,
                "result_payload": result_payload,
            }
        )
        headers = _auth_headers(private_key, provider_id, complete_payload)
        resp = client.post("/tasks/complete", content=complete_payload, headers=headers)
        self.assertEqual(resp.status_code, 200, f"Complete failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["status"], "success")
        return data

    def test_spec_compute_market_sender_pays_receiver(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        provider_priv, provider_pub, provider_id = _make_identity()
        _register(consumer_pub)
        _register(provider_pub)

        consumer_before = client.get(f"/balance/{consumer_id}").json()["balance_seconds"]
        provider_before = client.get(f"/balance/{provider_id}").json()["balance_seconds"]

        task_id = self._submit_spec_task(
            consumer_priv,
            consumer_id,
            instructions="compute this",
            bounty_ns=1_000_000_000,
            market="compute",
            payment_direction="sender_to_receiver",
            target_capability="text",
        )
        stored = db.get_task(task_id)
        self.assertEqual(stored["payload"], "compute this")
        self.assertEqual(stored["bounty"], 1.0)
        self.assertEqual(stored["model_requirement"], "text")

        self._bid(provider_priv, provider_id, task_id)
        result = self._complete(provider_priv, provider_id, task_id, "computed")
        self.assertEqual(result["earned"], 1.0)
        self.assertEqual(client.get(f"/balance/{consumer_id}").json()["balance_seconds"], consumer_before - 1.0)
        self.assertEqual(client.get(f"/balance/{provider_id}").json()["balance_seconds"], provider_before + 1.0)

    def test_spec_chat_market_targeted_zero_bounty(self):
        sender_priv, sender_pub, sender_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(sender_pub)
        _register(target_pub)

        sender_before = client.get(f"/balance/{sender_id}").json()["balance_seconds"]
        fake_ws = _FakeWebSocket()
        main.connected_nodes[target_id] = fake_ws
        try:
            task_id = self._submit_spec_task(
                sender_priv,
                sender_id,
                instructions="hello target",
                bounty_ns=0,
                market="chat",
                payment_direction="none",
                target_node=target_id,
            )
        finally:
            main.connected_nodes.pop(target_id, None)
        stored = db.get_task(task_id)
        self.assertEqual(stored["payload"], "hello target")
        self.assertEqual(stored["bounty"], 0.0)
        self.assertEqual(stored["target_node"], target_id)
        self.assertEqual(stored["status"], "assigned")
        self.assertEqual(stored["provider_id"], target_id)
        self.assertEqual(fake_ws.sent[0]["event"], "new_task")
        self.assertEqual(fake_ws.sent[0]["data"]["id"], task_id)
        self.assertEqual(client.get(f"/balance/{sender_id}").json()["balance_seconds"], sender_before)

    def test_spec_data_market_receiver_pays_sender_for_secret_data(self):
        seller_priv, seller_pub, seller_id = _make_identity()
        buyer_priv, buyer_pub, buyer_id = _make_identity()
        _register(seller_pub)
        _register(buyer_pub)

        seller_before = client.get(f"/balance/{seller_id}").json()["balance_seconds"]
        buyer_before = client.get(f"/balance/{buyer_id}").json()["balance_seconds"]
        task_id = self._submit_spec_task(
            seller_priv,
            seller_id,
            instructions="premium dataset",
            bounty_ns=500_000_000,
            market="data",
            payment_direction="receiver_to_sender",
            secret_data="encrypted-premium-data",
        )
        stored = db.get_task(task_id)
        self.assertEqual(stored["bounty"], -0.5)
        self.assertEqual(stored["result_payload"], "encrypted-premium-data")

        bid_data = self._bid(buyer_priv, buyer_id, task_id)
        self.assertEqual(bid_data["secret_data"], "encrypted-premium-data")
        result = self._complete(buyer_priv, buyer_id, task_id, "data received")
        self.assertEqual(result["earned"], -0.5)
        self.assertEqual(client.get(f"/balance/{seller_id}").json()["balance_seconds"], seller_before + 0.5)
        self.assertEqual(client.get(f"/balance/{buyer_id}").json()["balance_seconds"], buyer_before - 0.5)

    def _submit_offline_dm(self, sender_priv, sender_id, target_id, instructions="offline hello"):
        task_payload = json.dumps(
            {
                "source": {"node_id": sender_id},
                "intent": {"type": "conformance.request"},
                "task": {
                    "instructions": instructions,
                    "expected_output": {"result_type": "text"},
                },
                "economics": {
                    "bounty_ns": 0,
                    "currency": "MEP_NS",
                    "market": "chat",
                    "payment_direction": "none",
                },
                "routing": {"target_node_id": target_id},
            }
        )
        headers = _auth_headers(sender_priv, sender_id, task_payload)
        return client.post("/tasks/submit", content=task_payload, headers=headers)

    def test_zero_bounty_dm_to_offline_node_is_queued(self):
        sender_priv, sender_pub, sender_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(sender_pub)
        _register(target_pub)
        main.connected_nodes.pop(target_id, None)

        resp = self._submit_offline_dm(sender_priv, sender_id, target_id)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["queued_for"], target_id)
        stored = db.get_task(body["task_id"])
        self.assertEqual(stored["status"], "queued_dm")
        self.assertEqual(stored["target_node"], target_id)
        self.assertEqual(db.count_queued_dms_for_node(target_id), 1)

    def test_queued_dm_flushed_on_reconnect(self):
        import asyncio

        sender_priv, sender_pub, sender_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(sender_pub)
        _register(target_pub)
        main.connected_nodes.pop(target_id, None)

        resp = self._submit_offline_dm(sender_priv, sender_id, target_id, instructions="ping while away")
        task_id = resp.json()["task_id"]
        self.assertEqual(resp.json()["status"], "queued")

        # Target reconnects -> flush should deliver the queued DM and mark it assigned.
        fake_ws = _FakeWebSocket()
        asyncio.run(main._flush_queued_dms(target_id, fake_ws))

        self.assertEqual(len(fake_ws.sent), 1)
        self.assertEqual(fake_ws.sent[0]["event"], "new_task")
        self.assertEqual(fake_ws.sent[0]["data"]["id"], task_id)
        self.assertEqual(fake_ws.sent[0]["data"]["payload"], "ping while away")
        stored = db.get_task(task_id)
        self.assertEqual(stored["status"], "assigned")
        self.assertEqual(stored["provider_id"], target_id)
        self.assertEqual(db.count_queued_dms_for_node(target_id), 0)

    def test_queued_dm_flush_matches_live_targeted_shape(self):
        """A queued DM must be flushed with the same new_task event contract as a
        live targeted delivery, so offline recipients are not handed a thinner
        payload (regression guard for the store-and-forward shape mismatch)."""
        import asyncio

        sender_priv, sender_pub, sender_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(sender_pub)
        _register(target_pub)

        # 1) Live targeted delivery: target online, capture the new_task data.
        live_ws = _FakeWebSocket()
        main.connected_nodes[target_id] = live_ws
        try:
            live_resp = self._submit_offline_dm(sender_priv, sender_id, target_id, instructions="same body")
        finally:
            main.connected_nodes.pop(target_id, None)
        self.assertEqual(live_resp.json()["status"], "success")
        live_data = live_ws.sent[0]["data"]

        # 2) Queued delivery: target offline -> queue -> reconnect -> flush.
        main.connected_nodes.pop(target_id, None)
        queued_resp = self._submit_offline_dm(sender_priv, sender_id, target_id, instructions="same body")
        self.assertEqual(queued_resp.json()["status"], "queued")
        flush_ws = _FakeWebSocket()
        asyncio.run(main._flush_queued_dms(target_id, flush_ws))
        self.assertEqual(len(flush_ws.sent), 1)
        flushed_data = flush_ws.sent[0]["data"]

        # Core contract fields must be identical (task_id differs per submit).
        for field in (
            "consumer_id", "payload", "bounty", "status", "provider_id",
            "source", "intent", "task", "economics", "target_node",
            "model_requirement", "payload_uri",
        ):
            self.assertEqual(
                flushed_data.get(field), live_data.get(field),
                f"queued-flush field '{field}' diverged from live targeted delivery",
            )
        # source.node_id (used for reply routing) must survive store-and-forward.
        self.assertEqual(flushed_data["source"]["node_id"], sender_id)
        self.assertEqual(flushed_data["task"]["expected_output"], live_data["task"]["expected_output"])

    def test_dm_queue_full_rejected(self):
        sender_priv, sender_pub, sender_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(sender_pub)
        _register(target_pub)
        main.connected_nodes.pop(target_id, None)

        original_cap = main.MAX_QUEUED_DMS_PER_NODE
        main.MAX_QUEUED_DMS_PER_NODE = 1
        try:
            first = self._submit_offline_dm(sender_priv, sender_id, target_id, instructions="dm one")
            self.assertEqual(first.json()["status"], "queued")
            second = self._submit_offline_dm(sender_priv, sender_id, target_id, instructions="dm two")
            self.assertEqual(second.json()["status"], "error")
            self.assertIn("queue is full", second.json()["detail"])
        finally:
            main.MAX_QUEUED_DMS_PER_NODE = original_cap

    def test_targeted_dm_to_offline_node_errors_when_queue_disabled(self):
        sender_priv, sender_pub, sender_id = _make_identity()
        _, target_pub, target_id = _make_identity()
        _register(sender_pub)
        _register(target_pub)
        main.connected_nodes.pop(target_id, None)

        original = main.DM_QUEUE_ENABLED
        main.DM_QUEUE_ENABLED = False
        try:
            resp = self._submit_offline_dm(sender_priv, sender_id, target_id)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "error")
            self.assertIn("not currently connected", resp.json()["detail"])
        finally:
            main.DM_QUEUE_ENABLED = original

    def test_data_market_requires_secret_data(self):
        seller_priv, seller_pub, seller_id = _make_identity()
        _register(seller_pub)

        task_payload = json.dumps(
            {
                "source": {"node_id": seller_id},
                "intent": {"type": "conformance.request"},
                "task": {
                    "instructions": "premium dataset",
                    "expected_output": {"result_type": "text"},
                },
                "economics": {
                    "bounty_ns": 500_000_000,
                    "currency": "MEP_NS",
                    "market": "data",
                    "payment_direction": "receiver_to_sender",
                },
            }
        )
        headers = _auth_headers(seller_priv, seller_id, task_payload)
        resp = client.post("/tasks/submit", content=task_payload, headers=headers)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("require secret_data", resp.json()["detail"])


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
        self.assertIn(resp.json()["status"], ("success", "error", "queued"))

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

    def test_spec_shaped_task_bounty_ns_is_converted_to_seconds(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)

        resp = client.get(f"/balance/{consumer_id}")
        initial_balance = resp.json()["balance_seconds"]

        task_payload = json.dumps(
            {
                "source": {"node_id": consumer_id},
                "intent": {"type": "analysis.request"},
                "task": {
                    "instructions": "spec-shaped compute task",
                    "expected_output": {"result_type": "text"},
                },
                "economics": {
                    "bounty_ns": 1_000_000_000,
                    "currency": "MEP_NS",
                    "market": "compute",
                    "payment_direction": "sender_to_receiver",
                },
                "routing": {"target_capability": "text"},
            }
        )
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        with _interbot_validation(True):
            resp = client.post("/tasks/submit", content=task_payload, headers=headers)

        self.assertEqual(resp.status_code, 200, f"Submit failed: {resp.text}")
        task_id = resp.json()["task_id"]
        stored_task = db.get_task(task_id)
        self.assertEqual(stored_task["bounty"], 1.0)

        resp = client.get(f"/balance/{consumer_id}")
        self.assertEqual(resp.json()["balance_seconds"], initial_balance - 1.0)

    def test_rejects_obsolete_bounty_quanta_name(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)

        task_payload = json.dumps(
            {
                "source": {"node_id": consumer_id},
                "task": {
                    "instructions": "obsolete unit name",
                    "expected_output": {"result_type": "text"},
                },
                "economics": {
                    "bounty_quanta": 1_000_000_000,
                    "currency": "MEP_QUANTA",
                    "market": "compute",
                    "payment_direction": "sender_to_receiver",
                },
            }
        )
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        with _interbot_validation(True):
            resp = client.post("/tasks/submit", content=task_payload, headers=headers)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("MEP_NS", resp.json()["detail"])


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

    def test_registry_update_can_persist_x25519_public_key(self):
        priv, pub_pem, node_id = _make_identity()
        _register(pub_pem)
        update_payload = json.dumps({"alias": "diag-node", "x25519_public_key": "updated-encpub"})
        headers = _auth_headers(priv, node_id, update_payload)

        update_resp = client.post("/registry/update", content=update_payload, headers=headers)
        self.assertEqual(update_resp.status_code, 200, f"Registry update failed: {update_resp.text}")

        registry = db.get_registry(node_id)
        self.assertIsNotNone(registry)
        self.assertEqual(registry["x25519_public_key"], "updated-encpub")


class TestOnboardDiagnose(unittest.TestCase):
    def setUp(self):
        main.onboard_diagnosis_counts.clear()
        main.onboard_diagnosis_total = 0

    def test_detects_auth_401_signature_or_timestamp(self):
        payload = {
            "node_id": "node_test",
            "auth_status": "401",
            "registered": True,
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "auth_401_signature_or_timestamp")
        self.assertEqual(data["severity"], "high")
        self.assertGreaterEqual(data["telemetry"]["total_requests"], 1)

    def test_detects_ghost_online_without_ws(self):
        payload = {
            "node_id": "node_test",
            "registered": True,
            "ws_connected": False,
            "heartbeat_seconds_ago": 15,
            "auth_status": "ok",
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "ghost_online_no_ws_presence")
        self.assertEqual(data["severity"], "high")

    def test_returns_healthy_or_insufficient_signal_when_no_fault(self):
        payload = {
            "node_id": "node_test",
            "registered": True,
            "ws_connected": True,
            "auth_status": "ok",
            "dm_status": "ok",
            "listener_contract_ok": True,
            "ai_configured": True,
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "healthy_or_insufficient_signal")
        self.assertEqual(data["severity"], "info")

    def test_detects_auth_403_unregistered_or_policy(self):
        payload = {
            "node_id": "node_test",
            "auth_status": "403",
            "registered": False,
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "auth_403_unregistered_or_policy")
        self.assertEqual(data["severity"], "high")

    def test_detects_dm_pending_target_offline_or_route_issue(self):
        payload = {
            "node_id": "node_test",
            "registered": True,
            "ws_connected": True,
            "auth_status": "ok",
            "dm_status": "pending",
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "dm_pending_target_offline_or_route_issue")
        self.assertEqual(data["severity"], "medium")

    def test_detects_listener_payload_contract_mismatch(self):
        payload = {
            "node_id": "node_test",
            "registered": True,
            "ws_connected": True,
            "auth_status": "ok",
            "dm_status": "ok",
            "listener_contract_ok": False,
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "listener_payload_contract_mismatch")
        self.assertEqual(data["severity"], "medium")

    def test_detects_heartbeat_interval_or_clock_drift(self):
        payload = {
            "node_id": "node_test",
            "registered": True,
            "ws_connected": True,
            "auth_status": "ok",
            "dm_status": "ok",
            "listener_contract_ok": True,
            "ai_configured": True,
            "clock_skew_seconds": 400,
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "heartbeat_interval_or_clock_drift")
        self.assertEqual(data["severity"], "medium")

    def test_detects_ai_provider_config_invalid(self):
        payload = {
            "node_id": "node_test",
            "registered": True,
            "ws_connected": True,
            "auth_status": "ok",
            "dm_status": "ok",
            "listener_contract_ok": True,
            "ai_configured": False,
        }
        resp = client.post("/onboard/diagnose", json=payload)
        self.assertEqual(resp.status_code, 200, f"Diagnose failed: {resp.text}")
        data = resp.json()
        self.assertEqual(data["root_cause"], "ai_provider_config_invalid")
        self.assertEqual(data["severity"], "low")


class TestRegistryHeartbeatPresence(unittest.TestCase):
    def test_heartbeat_online_without_websocket_forces_offline(self):
        node_priv, node_pub, node_id = _make_identity()
        _register(node_pub)
        heartbeat_payload = json.dumps({"availability": "online"})
        headers = _auth_headers(node_priv, node_id, heartbeat_payload)
        heartbeat_response = client.post("/registry/heartbeat", content=heartbeat_payload, headers=headers)
        self.assertEqual(heartbeat_response.status_code, 200, f"Heartbeat failed: {heartbeat_response.text}")
        self.assertEqual(heartbeat_response.json()["availability"], "offline")

        registry_response = client.get(f"/registry/{node_id}")
        self.assertEqual(registry_response.status_code, 200, f"Registry read failed: {registry_response.text}")
        self.assertEqual(registry_response.json()["availability"], "offline")


class TestMeshAssembly(unittest.TestCase):
    def setUp(self):
        conn = db._get_conn()
        conn.execute("UPDATE agent_registry SET availability = 'offline'")
        conn.commit()
        db._release_conn(conn)
        main.mesh_assemblies.clear()
        main.connected_nodes.clear()

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
        # Mesh role selection treats websocket presence as source of truth.
        main.connected_nodes[node_id] = object()
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
