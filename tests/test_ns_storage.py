"""Tests for PR 2: additive ns storage, backfill, and dual-write."""

import os
import tempfile
import unittest

os.environ["MEP_SQLITE_PATH"] = os.path.join(tempfile.gettempdir(), "mep_test_ns_storage.db")
os.environ.setdefault("MEP_DATABASE_URL", "")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "hub"))

import db


class TestNSSchemaMigration(unittest.TestCase):
    def setUp(self):
        self.test_db = os.environ["MEP_SQLITE_PATH"]
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_balance_ns_column_exists(self):
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(ledger)")
        columns = [row[1] for row in cursor.fetchall()]
        self.assertIn("balance_ns", columns)
        db._release_conn(conn)

    def test_bounty_ns_column_exists(self):
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        self.assertIn("bounty_ns", columns)
        db._release_conn(conn)

    def test_amount_ns_column_exists(self):
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(escrows)")
        columns = [row[1] for row in cursor.fetchall()]
        self.assertIn("amount_ns", columns)
        db._release_conn(conn)

    def test_ns_columns_have_integer_affinity(self):
        conn = db._get_conn()
        cursor = conn.cursor()
        for table, column in [("ledger", "balance_ns"), ("tasks", "bounty_ns"), ("escrows", "amount_ns")]:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = {row[1]: row[2].upper() for row in cursor.fetchall()}
            self.assertIn("INT", columns.get(column, ""), f"{table}.{column} must have INTEGER affinity")
        db._release_conn(conn)


class TestFinancialBackfill(unittest.TestCase):
    def setUp(self):
        self.test_db = os.environ["MEP_SQLITE_PATH"]
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_exact_legacy_values_backfill_to_ns(self):
        node_id = "test_node"
        db.register_node(node_id, "test_pem")
        db.approve_registration(node_id, "admin", initial_balance=10.0)
        report = db.audit_financial_ns_backfill()
        ledger_rows = [r for r in report if r["table"] == "ledger" and r["row_id"] == node_id]
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], "exact")
        self.assertEqual(ledger_rows[0]["ns_expected"], "10000000000")

    def test_non_exact_legacy_values_reported_as_rounded(self):
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO ledger (node_id, pub_pem, balance) VALUES (?, ?, ?)", ("bad_node", "pem", 0.0000000015))
        conn.commit()
        db._release_conn(conn)
        report = db.audit_financial_ns_backfill()
        bad_rows = [r for r in report if r["table"] == "ledger" and r["row_id"] == "bad_node"]
        self.assertEqual(len(bad_rows), 1)
        self.assertEqual(bad_rows[0]["status"], "rounded")

    def test_backfill_populates_exact_values_only(self):
        node_id = "test_node"
        db.register_node(node_id, "test_pem")
        db.approve_registration(node_id, "admin", initial_balance=5.0)
        report = db.backfill_financial_ns_columns()
        ledger_rows = [r for r in report if r["table"] == "ledger" and r["row_id"] == node_id]
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], "exact")
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        ns_value = cursor.fetchone()
        self.assertEqual(ns_value[0], 5000000000)
        db._release_conn(conn)


class TestDualWriteConsistency(unittest.TestCase):
    def setUp(self):
        self.test_db = os.environ["MEP_SQLITE_PATH"]
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_approve_registration_dual_writes_balance_and_balance_ns(self):
        node_id = "test_node"
        db.register_node(node_id, "test_pem")
        db.approve_registration(node_id, "admin", initial_balance=10.0)
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 10.0)
        self.assertEqual(balance_ns, 10000000000)
        db._release_conn(conn)

    def test_set_balance_dual_writes_both_columns(self):
        node_id = "test_node"
        db.register_node(node_id, "test_pem")
        db.approve_registration(node_id, "admin", initial_balance=5.0)
        db.set_balance(node_id, 15.0)
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 15.0)
        self.assertEqual(balance_ns, 15000000000)
        db._release_conn(conn)

    def test_add_balance_dual_writes_both_columns(self):
        node_id = "test_node"
        db.register_node(node_id, "test_pem")
        db.approve_registration(node_id, "admin", initial_balance=5.0)
        db.add_balance(node_id, 7.0)
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 12.0)
        self.assertEqual(balance_ns, 12000000000)
        db._release_conn(conn)

    def test_deduct_balance_dual_writes_both_columns(self):
        node_id = "test_node"
        db.register_node(node_id, "test_pem")
        db.approve_registration(node_id, "admin", initial_balance=10.0)
        db.deduct_balance(node_id, 3.0)
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 7.0)
        self.assertEqual(balance_ns, 7000000000)
        db._release_conn(conn)

    def test_create_task_dual_writes_bounty_and_bounty_ns(self):
        task_id = "test_task"
        db.create_task(task_id, "consumer", "payload", 5.0, "bidding", None, None, 0.0)
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT bounty, bounty_ns FROM tasks WHERE task_id = ?", (task_id,))
        bounty, bounty_ns = cursor.fetchone()
        self.assertEqual(bounty, 5.0)
        self.assertEqual(bounty_ns, 5000000000)
        db._release_conn(conn)

    def test_create_escrow_dual_writes_amount_and_amount_ns(self):
        task_id = "test_task"
        db.create_escrow(task_id, "consumer", 3.0, 0.0)
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT amount, amount_ns FROM escrows WHERE task_id = ?", (task_id,))
        amount, amount_ns = cursor.fetchone()
        self.assertEqual(amount, 3.0)
        self.assertEqual(amount_ns, 3000000000)
        db._release_conn(conn)


if __name__ == "__main__":
    unittest.main()
