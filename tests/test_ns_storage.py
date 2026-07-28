"""Tests for PR 2: additive ns storage, backfill, and dual-write."""

import os
import tempfile
import unittest
import importlib

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "hub"))


class TestNSSchemaMigration(unittest.TestCase):
    def setUp(self):
        self.test_db = os.path.join(tempfile.gettempdir(), f"mep_test_ns_storage_{self.__class__.__name__}.db")
        os.environ["MEP_SQLITE_PATH"] = self.test_db
        os.environ.setdefault("MEP_DATABASE_URL", "")
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        import db
        importlib.reload(db)
        self.db = db
        db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_balance_ns_column_exists(self):
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(ledger)")
        columns = [row[1] for row in cursor.fetchall()]
        self.assertIn("balance_ns", columns)
        self.db._release_conn(conn)

    def test_bounty_ns_column_exists(self):
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        self.assertIn("bounty_ns", columns)
        self.db._release_conn(conn)

    def test_amount_ns_column_exists(self):
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(escrows)")
        columns = [row[1] for row in cursor.fetchall()]
        self.assertIn("amount_ns", columns)
        self.db._release_conn(conn)

    def test_ns_columns_have_integer_affinity(self):
        conn = self.db._get_conn()
        cursor = conn.cursor()
        for table, column in [("ledger", "balance_ns"), ("tasks", "bounty_ns"), ("escrows", "amount_ns")]:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = {row[1]: row[2].upper() for row in cursor.fetchall()}
            self.assertIn("INT", columns.get(column, ""), f"{table}.{column} must have INTEGER affinity")
        self.db._release_conn(conn)


class TestFinancialBackfill(unittest.TestCase):
    def setUp(self):
        self.test_db = os.path.join(tempfile.gettempdir(), f"mep_test_ns_storage_{self.__class__.__name__}.db")
        os.environ["MEP_SQLITE_PATH"] = self.test_db
        os.environ.setdefault("MEP_DATABASE_URL", "")
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        import db
        importlib.reload(db)
        self.db = db
        db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_exact_legacy_values_backfill_to_ns(self):
        node_id = "test_node"
        self.db.register_node(node_id, "test_pem")
        self.db.approve_registration(node_id, "admin", initial_balance=10.0)
        report = self.db.audit_financial_ns_backfill()
        ledger_rows = [r for r in report if r["table"] == "ledger" and r["row_id"] == node_id]
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], "exact")
        self.assertEqual(ledger_rows[0]["ns_expected"], "10000000000")

    def test_non_exact_legacy_values_reported_as_rounded(self):
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO ledger (node_id, pub_pem, balance) VALUES (?, ?, ?)", ("bad_node", "pem", 0.0000000015))
        conn.commit()
        self.db._release_conn(conn)
        report = self.db.audit_financial_ns_backfill()
        bad_rows = [r for r in report if r["table"] == "ledger" and r["row_id"] == "bad_node"]
        self.assertEqual(len(bad_rows), 1)
        self.assertEqual(bad_rows[0]["status"], "rounded")

    def test_backfill_populates_exact_values_only(self):
        node_id = "test_node"
        self.db.register_node(node_id, "test_pem")
        self.db.approve_registration(node_id, "admin", initial_balance=5.0)
        report = self.db.backfill_financial_ns_columns()
        ledger_rows = [r for r in report if r["table"] == "ledger" and r["row_id"] == node_id]
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], "exact")
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        ns_value = cursor.fetchone()
        self.assertEqual(ns_value[0], 5000000000)
        self.db._release_conn(conn)


class TestDualWriteConsistency(unittest.TestCase):
    def setUp(self):
        self.test_db = os.path.join(tempfile.gettempdir(), f"mep_test_ns_storage_{self.__class__.__name__}.db")
        os.environ["MEP_SQLITE_PATH"] = self.test_db
        os.environ.setdefault("MEP_DATABASE_URL", "")
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        import db
        importlib.reload(db)
        self.db = db
        db.init_db()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_approve_registration_dual_writes_balance_and_balance_ns(self):
        node_id = "test_node"
        self.db.register_node(node_id, "test_pem")
        self.db.approve_registration(node_id, "admin", initial_balance=10.0)
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 10.0)
        self.assertEqual(balance_ns, 10000000000)
        self.db._release_conn(conn)

    def test_set_balance_dual_writes_both_columns(self):
        node_id = "test_node"
        self.db.register_node(node_id, "test_pem")
        self.db.approve_registration(node_id, "admin", initial_balance=5.0)
        self.db.set_balance(node_id, 15.0)
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 15.0)
        self.assertEqual(balance_ns, 15000000000)
        self.db._release_conn(conn)

    def test_add_balance_dual_writes_both_columns(self):
        node_id = "test_node"
        self.db.register_node(node_id, "test_pem")
        self.db.approve_registration(node_id, "admin", initial_balance=5.0)
        self.db.add_balance(node_id, 7.0)
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 12.0)
        self.assertEqual(balance_ns, 12000000000)
        self.db._release_conn(conn)

    def test_deduct_balance_dual_writes_both_columns(self):
        node_id = "test_node"
        self.db.register_node(node_id, "test_pem")
        self.db.approve_registration(node_id, "admin", initial_balance=10.0)
        self.db.deduct_balance(node_id, 3.0)
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
        balance, balance_ns = cursor.fetchone()
        self.assertEqual(balance, 7.0)
        self.assertEqual(balance_ns, 7000000000)
        self.db._release_conn(conn)

    def test_create_task_dual_writes_bounty_and_bounty_ns(self):
        task_id = "test_task"
        self.db.create_task(task_id, "consumer", "payload", 5.0, "bidding", None, None, 0.0)
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT bounty, bounty_ns FROM tasks WHERE task_id = ?", (task_id,))
        bounty, bounty_ns = cursor.fetchone()
        self.assertEqual(bounty, 5.0)
        self.assertEqual(bounty_ns, 5000000000)
        self.db._release_conn(conn)

    def test_create_escrow_dual_writes_amount_and_amount_ns(self):
        task_id = "test_task"
        self.db.create_escrow(task_id, "consumer", 3.0, 0.0)
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT amount, amount_ns FROM escrows WHERE task_id = ?", (task_id,))
        amount, amount_ns = cursor.fetchone()
        self.assertEqual(amount, 3.0)
        self.assertEqual(amount_ns, 3000000000)
        self.db._release_conn(conn)

    def test_market_price_samples_include_only_released_compute_escrow(self):
        self.db.create_task(
            "released_review",
            "consumer",
            "review",
            1.0,
            "completed",
            None,
            "code_review",
            100.0,
        )
        self.db.create_escrow("released_review", "consumer", 1.0, 100.0)
        self.db.create_task(
            "released_test",
            "consumer",
            "test",
            2.0,
            "completed",
            None,
            "testing",
            101.0,
        )
        self.db.create_escrow("released_test", "consumer", 2.0, 101.0)
        self.db.create_task(
            "held_review",
            "consumer",
            "review",
            3.0,
            "completed",
            None,
            "code_review",
            102.0,
        )
        self.db.create_escrow("held_review", "consumer", 3.0, 102.0)

        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE escrows SET status = 'released' WHERE task_id IN (?, ?)",
            ("released_review", "released_test"),
        )
        conn.commit()
        self.db._release_conn(conn)

        all_samples = self.db.get_settled_compute_price_samples(since_ts=0, limit=10)
        review_samples = self.db.get_settled_compute_price_samples(
            capability="CODE_REVIEW",
            since_ts=0,
            limit=10,
        )

        self.assertCountEqual(all_samples, [1_000_000_000, 2_000_000_000])
        self.assertEqual(review_samples, [1_000_000_000])


if __name__ == "__main__":
    unittest.main()
