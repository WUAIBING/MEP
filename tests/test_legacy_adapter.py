"""Tests for PR 4: legacy adapter layer + regression coverage.

These tests lock the design-lock acceptance criterion from #223/#224:

    Legacy financial endpoint handlers contain no direct financial float
    arithmetic. Money operations route through the canonical ns-first internal
    path (``hub/money.py``), with legacy float translation confined to the
    response boundary.

Two things are verified:
- the canonical money path treats the integer ns column as the source of truth
- the legacy float surface and the v2 ns surface stay consistent because they
  are both derived from that single canonical path
"""

import base64
import json
import os
import sys
import tempfile
import time
import unittest
import uuid

# Dedicated SQLite file; tests only ever read nodes/tasks they create, so they
# are correct regardless of which test module initialised the DB first.
_test_db = os.path.join(tempfile.gettempdir(), "mep_test_legacy_adapter.db")
os.environ["MEP_SQLITE_PATH"] = _test_db
os.environ.setdefault("MEP_DATABASE_URL", "")  # force SQLite
os.environ.setdefault("MEP_ADMIN_KEY", "test-admin-key")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "hub"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

import db  # noqa: E402
import money  # noqa: E402
from main import app  # noqa: E402
from nanoseconds import NanosecondsError, NS_PER_SECOND  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# Auth helpers (mirror test_hub_api.py)
# ---------------------------------------------------------------------------
def _make_identity():
    private_key = Ed25519PrivateKey.generate()
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    from auth import derive_node_id
    return private_key, pub_pem, derive_node_id(pub_pem)


def _auth_headers(private_key, node_id: str, payload_str: str) -> dict:
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
    assert resp.status_code == 200, resp.text
    data = resp.json()
    if data["status"] == "pending":
        approve = client.post(
            "/admin/approve-registration",
            json={"node_id": data["node_id"]},
            headers={"x-mep-admin-key": "test-admin-key"},
        )
        assert approve.status_code == 200, approve.text
        data = approve.json()
    return data


def _set_balance_ns_raw(node_id: str, balance_ns: int):
    """Force the canonical ns column to a value, leaving the legacy float alone."""
    conn = db._get_conn()
    cursor = conn.cursor()
    placeholder = "%s" if db._is_postgres() else "?"
    cursor.execute(
        f"UPDATE ledger SET balance_ns = {placeholder} WHERE node_id = {placeholder}",
        (balance_ns, node_id),
    )
    conn.commit()
    db._release_conn(conn)


# ---------------------------------------------------------------------------
# Canonical money path (pure unit tests)
# ---------------------------------------------------------------------------
class TestMoneyCanonicalPath(unittest.TestCase):
    def test_prefers_ns_column_over_float(self):
        # ns column is the source of truth even when the float disagrees.
        self.assertEqual(
            money.canonical_ns(10.0, 7_000_000_000, "balance", allow_negative=False),
            7_000_000_000,
        )

    def test_falls_back_to_exact_float_when_ns_null(self):
        self.assertEqual(
            money.canonical_ns(5.0, None, "balance", allow_negative=False),
            5_000_000_000,
        )

    def test_returns_none_for_nonexact_float_without_ns(self):
        # Pre-migration row whose float cannot be represented exactly in ns.
        self.assertIsNone(
            money.canonical_ns(0.0000000015, None, "balance", allow_negative=False)
        )

    def test_raises_for_negative_ns_when_not_allowed(self):
        with self.assertRaises(NanosecondsError):
            money.canonical_ns(None, -5_000_000_000, "balance", allow_negative=False)

    def test_allows_negative_ns_for_bounty(self):
        self.assertEqual(
            money.canonical_ns(None, -5_000_000_000, "bounty", allow_negative=True),
            -5_000_000_000,
        )

    def test_to_legacy_seconds_prefers_ns(self):
        self.assertEqual(
            money.to_legacy_seconds(10.0, 7_000_000_000, "balance", allow_negative=False),
            7.0,
        )

    def test_to_legacy_seconds_falls_back_to_raw_float(self):
        # No canonical ns truth available -> return the stored legacy float as-is.
        self.assertEqual(
            money.to_legacy_seconds(0.0000000015, None, "balance", allow_negative=False),
            0.0000000015,
        )

    def test_to_v2_ns_string_prefers_ns(self):
        self.assertEqual(
            money.to_v2_ns_string(10.0, 7_000_000_000, "balance", allow_negative=False),
            "7000000000",
        )

    def test_to_v2_ns_string_falls_back_to_exact_convert(self):
        self.assertEqual(
            money.to_v2_ns_string(5.0, None, "balance", allow_negative=False),
            "5000000000",
        )

    def test_legacy_and_v2_are_consistent(self):
        # Both surfaces derive from the same canonical ns value.
        legacy = money.to_legacy_seconds(0.0, 2_500_000_000, "amount", allow_negative=False)
        v2 = money.to_v2_ns_string(0.0, 2_500_000_000, "amount", allow_negative=False)
        self.assertEqual(int(v2), round(legacy * NS_PER_SECOND))
        self.assertEqual(int(v2), 2_500_000_000)


# ---------------------------------------------------------------------------
# Endpoint regression coverage (legacy vs v2 consistency)
# ---------------------------------------------------------------------------
class TestBalanceAdapter(unittest.TestCase):
    def test_legacy_and_v2_balance_consistent(self):
        _, pub_pem, node_id = _make_identity()
        _register(pub_pem)

        legacy = client.get(f"/balance/{node_id}")
        v2 = client.get(f"/v2/balance/{node_id}")
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(v2.status_code, 200, v2.text)

        balance_seconds = legacy.json()["balance_seconds"]
        balance_ns = int(v2.json()["balance_ns"])
        self.assertEqual(balance_ns, round(balance_seconds * NS_PER_SECOND))
        # Approved nodes start with 10 SECONDS.
        self.assertEqual(balance_ns, 10_000_000_000)

    def test_balance_uses_ns_column_as_source_of_truth(self):
        _, pub_pem, node_id = _make_identity()
        _register(pub_pem)

        # Diverge the canonical ns column from the legacy float. Both endpoints
        # must now reflect the ns value, proving they read through the ns path.
        _set_balance_ns_raw(node_id, 7_000_000_000)

        legacy = client.get(f"/balance/{node_id}").json()
        v2 = client.get(f"/v2/balance/{node_id}").json()
        self.assertEqual(legacy["balance_seconds"], 7.0)
        self.assertEqual(v2["balance_ns"], "7000000000")

    def test_unknown_node_returns_404_on_both_surfaces(self):
        self.assertEqual(client.get("/balance/does-not-exist").status_code, 404)
        self.assertEqual(client.get("/v2/balance/does-not-exist").status_code, 404)


class TestEscrowAdapter(unittest.TestCase):
    def test_escrow_amount_ns_consistent_with_bounty(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)

        task_payload = json.dumps({"consumer_id": consumer_id, "payload": "work", "bounty": 3.0})
        headers = _auth_headers(consumer_priv, consumer_id, task_payload)
        submit = client.post("/tasks/submit", content=task_payload, headers=headers)
        self.assertEqual(submit.status_code, 200, submit.text)
        task_id = submit.json()["task_id"]

        # Escrow held for the bounty; v2 escrow amount is the canonical ns value.
        v2_escrow = client.get(
            f"/v2/escrows/{task_id}",
            headers=_auth_headers(consumer_priv, consumer_id, ""),
        )
        self.assertEqual(v2_escrow.status_code, 200, v2_escrow.text)
        self.assertEqual(v2_escrow.json()["amount_ns"], "3000000000")

        # Consumer balance dropped by the bounty on both surfaces (10 - 3 = 7).
        legacy_balance = client.get(f"/balance/{consumer_id}").json()["balance_seconds"]
        v2_balance = client.get(f"/v2/balance/{consumer_id}").json()["balance_ns"]
        self.assertEqual(legacy_balance, 7.0)
        self.assertEqual(v2_balance, "7000000000")


class TestTaskResultAdapter(unittest.TestCase):
    def test_legacy_and_v2_task_result_bounty_consistent(self):
        consumer_priv, consumer_pub, consumer_id = _make_identity()
        _register(consumer_pub)

        task_id = f"legacy-adapter-result-{uuid.uuid4()}"
        db.create_task(
            task_id, consumer_id, "payload", 2.0, "completed", None, None, time.time(),
            result_payload="done",
        )

        legacy = client.get(
            f"/tasks/result/{task_id}",
            headers=_auth_headers(consumer_priv, consumer_id, ""),
        )
        v2 = client.get(
            f"/v2/tasks/{task_id}/result",
            headers=_auth_headers(consumer_priv, consumer_id, ""),
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(v2.status_code, 200, v2.text)

        legacy_bounty = legacy.json()["bounty"]
        v2_bounty_ns = int(v2.json()["bounty_ns"])
        self.assertEqual(legacy_bounty, 2.0)
        self.assertEqual(v2_bounty_ns, 2_000_000_000)
        self.assertEqual(v2_bounty_ns, round(legacy_bounty * NS_PER_SECOND))


if __name__ == "__main__":
    unittest.main()
