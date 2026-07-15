import sqlite3
import os
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

from nanoseconds import (
    NS_PER_SECOND,
    NanosecondsError,
    legacy_seconds_to_ns,
    ns_to_legacy_seconds,
)

try:
    import psycopg2
    from psycopg2 import pool
except ImportError:
    psycopg2 = None

DB_FILE = os.getenv("MEP_SQLITE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db"))
DB_URL = os.getenv("MEP_DATABASE_URL")
PG_POOL_MIN = int(os.getenv("MEP_PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.getenv("MEP_PG_POOL_MAX", "5"))
_pg_pool: Optional["pool.SimpleConnectionPool"] = None

def _is_postgres() -> bool:
    return bool(DB_URL)

def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required for Postgres")
        _pg_pool = pool.SimpleConnectionPool(PG_POOL_MIN, PG_POOL_MAX, DB_URL)
    return _pg_pool

def _get_conn():
    if _is_postgres():
        return _get_pg_pool().getconn()
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def _release_conn(conn):
    if _is_postgres():
        _get_pg_pool().putconn(conn)
    else:
        conn.close()

def _row_to_dict(cursor, row):
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


FINANCIAL_NS_BACKFILL_REPORT: list[dict] = []


def _sql_placeholder() -> str:
    return "%s" if _is_postgres() else "?"


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    if _is_postgres():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            (table_name, column_name),
        )
        return bool(cursor.fetchone())
    cursor.execute(f"PRAGMA table_info({table_name})")
    return column_name in [row[1] for row in cursor.fetchall()]


def _ensure_integer_ns_column(cursor, table_name: str, column_name: str):
    if _column_exists(cursor, table_name, column_name):
        return
    if _is_postgres():
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} BIGINT")
    else:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} INTEGER")


def _ensure_ledger_balance_ns_column(cursor):
    _ensure_integer_ns_column(cursor, "ledger", "balance_ns")


def _ensure_tasks_bounty_ns_column(cursor):
    _ensure_integer_ns_column(cursor, "tasks", "bounty_ns")


def _ensure_escrows_amount_ns_column(cursor):
    _ensure_integer_ns_column(cursor, "escrows", "amount_ns")


def _assert_financial_ns_column_affinity(cursor):
    """Assert schema-level integer affinity for additive ns columns."""

    expected = [
        ("ledger", "balance_ns"),
        ("tasks", "bounty_ns"),
        ("escrows", "amount_ns"),
    ]
    if _is_postgres():
        for table_name, column_name in expected:
            cursor.execute(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
                """,
                (table_name, column_name),
            )
            row = cursor.fetchone()
            if not row or row[0] not in ("bigint", "integer"):
                raise RuntimeError(f"{table_name}.{column_name} must be integer-backed")
        return

    for table_name, column_name in expected:
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = {row[1]: row[2].upper() for row in cursor.fetchall()}
        if "INT" not in columns.get(column_name, ""):
            raise RuntimeError(f"{table_name}.{column_name} must have INTEGER affinity")


def _legacy_seconds_to_ns_exact(value, column_name: str, *, allow_negative: bool) -> int:
    ns = legacy_seconds_to_ns(value, column_name)
    if ns < 0 and not allow_negative:
        raise NanosecondsError(f"{column_name} cannot be negative")
    return ns


def _financial_pair(value, column_name: str, *, allow_negative: bool) -> tuple[int, float]:
    ns = _legacy_seconds_to_ns_exact(value, column_name, allow_negative=allow_negative)
    return ns, ns_to_legacy_seconds(ns)


def _cursor_add_balance(cursor, node_id: str, amount: float) -> int:
    amount_ns, amount = _financial_pair(amount, "amount", allow_negative=True)
    if _is_postgres():
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance + %s,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance + %s) * 1000000000 AS BIGINT) ELSE balance_ns + %s END
            WHERE node_id = %s
            """,
            (amount, amount, amount_ns, node_id),
        )
    else:
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance + ?,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance + ?) * 1000000000 AS INTEGER) ELSE balance_ns + ? END
            WHERE node_id = ?
            """,
            (amount, amount, amount_ns, node_id),
        )
    return int(cursor.rowcount or 0)


def _cursor_deduct_balance(cursor, node_id: str, amount: float) -> int:
    amount_ns, amount = _financial_pair(amount, "amount", allow_negative=False)
    if _is_postgres():
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance - %s,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance - %s) * 1000000000 AS BIGINT) ELSE balance_ns - %s END
            WHERE node_id = %s AND balance >= %s AND (balance_ns IS NULL OR balance_ns >= %s)
            """,
            (amount, amount, amount_ns, node_id, amount, amount_ns),
        )
    else:
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance - ?,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance - ?) * 1000000000 AS INTEGER) ELSE balance_ns - ? END
            WHERE node_id = ? AND balance >= ? AND (balance_ns IS NULL OR balance_ns >= ?)
            """,
            (amount, amount, amount_ns, node_id, amount, amount_ns),
        )
    return int(cursor.rowcount or 0)


def _cursor_insert_held_escrow(
    cursor,
    task_id: str,
    consumer_id: str,
    provider_id: Optional[str],
    amount: float,
    created_at: float,
    updated_at: float,
) -> int:
    amount_ns, amount = _financial_pair(amount, "amount", allow_negative=False)
    if _is_postgres():
        cursor.execute(
            """
            INSERT INTO escrows (task_id, consumer_id, provider_id, amount, amount_ns, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_id) DO NOTHING
            """,
            (task_id, consumer_id, provider_id, amount, amount_ns, "held", created_at, updated_at),
        )
    else:
        cursor.execute(
            """
            INSERT OR IGNORE INTO escrows (task_id, consumer_id, provider_id, amount, amount_ns, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, consumer_id, provider_id, amount, amount_ns, "held", created_at, updated_at),
        )
    return int(cursor.rowcount or 0)


def _coerce_ns_from_row(legacy_value, ns_value, column_name: str, *, allow_negative: bool) -> int:
    if ns_value is not None:
        if isinstance(ns_value, bool):
            raise NanosecondsError(f"{column_name}_ns must be an integer")
        ns_int = int(ns_value)
        if ns_int != ns_value:
            raise NanosecondsError(f"{column_name}_ns must be a whole integer")
        if ns_int < 0 and not allow_negative:
            raise NanosecondsError(f"{column_name}_ns cannot be negative")
        return ns_int
    return _legacy_seconds_to_ns_exact(legacy_value, column_name, allow_negative=allow_negative)


def _audit_financial_value(
    table_name: str,
    row_id,
    column_name: str,
    legacy_value,
    ns_actual,
    *,
    allow_negative: bool,
) -> dict:
    row = {
        "table": table_name,
        "row_id": str(row_id),
        "column": column_name,
        "float_value": str(legacy_value),
        "ns_expected": "",
        "ns_actual": "" if ns_actual is None else str(ns_actual),
        "status": "exact",
    }
    try:
        decimal_value = Decimal(str(legacy_value))
    except (InvalidOperation, ValueError):
        row["status"] = "nonsense"
        return row
    if not decimal_value.is_finite() or (decimal_value < 0 and not allow_negative):
        row["status"] = "nonsense"
        return row
    ns_decimal = decimal_value * Decimal(NS_PER_SECOND)
    integral = ns_decimal.to_integral_value()
    row["ns_expected"] = str(int(integral)) if ns_decimal == integral else format(ns_decimal, "f")
    if ns_decimal != integral:
        row["status"] = "rounded"
    return row


_FINANCIAL_NS_BACKFILL_CONFIG = [
    ("ledger", "node_id", "balance", "balance_ns", False),
    ("tasks", "task_id", "bounty", "bounty_ns", True),
    ("escrows", "task_id", "amount", "amount_ns", False),
]


def audit_financial_ns_backfill() -> list[dict]:
    """Return a structured report for legacy REAL -> integer ns conversion."""

    conn = _get_conn()
    cursor = conn.cursor()
    report: list[dict] = []
    try:
        for table_name, id_column, real_column, ns_column, allow_negative in _FINANCIAL_NS_BACKFILL_CONFIG:
            placeholder = ""  # Keeps f-string query construction explicit below.
            del placeholder
            cursor.execute(f"SELECT {id_column}, {real_column}, {ns_column} FROM {table_name}")
            for row_id, legacy_value, ns_actual in cursor.fetchall():
                report.append(
                    _audit_financial_value(
                        table_name,
                        row_id,
                        real_column,
                        legacy_value,
                        ns_actual,
                        allow_negative=allow_negative,
                    )
                )
        return report
    finally:
        _release_conn(conn)


def backfill_financial_ns_columns() -> list[dict]:
    """Populate additive ns columns for exact legacy values and report all rows."""

    conn = _get_conn()
    cursor = conn.cursor()
    report: list[dict] = []
    try:
        for table_name, id_column, real_column, ns_column, allow_negative in _FINANCIAL_NS_BACKFILL_CONFIG:
            cursor.execute(f"SELECT {id_column}, {real_column}, {ns_column} FROM {table_name}")
            rows = cursor.fetchall()
            for row_id, legacy_value, ns_actual in rows:
                audit_row = _audit_financial_value(
                    table_name,
                    row_id,
                    real_column,
                    legacy_value,
                    ns_actual,
                    allow_negative=allow_negative,
                )
                report.append(audit_row)
                if audit_row["status"] != "exact":
                    continue
                expected_ns = int(audit_row["ns_expected"])
                if ns_actual is not None and int(ns_actual) == expected_ns:
                    continue
                placeholder = _sql_placeholder()
                cursor.execute(
                    f"UPDATE {table_name} SET {ns_column} = {placeholder} WHERE {id_column} = {placeholder}",
                    (expected_ns, row_id),
                )
        conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)


def validate_financial_ns_storage() -> list[dict]:
    """Report SQLite rows whose ns columns are not stored as whole integers."""

    if _is_postgres():
        return []
    conn = _get_conn()
    cursor = conn.cursor()
    issues: list[dict] = []
    try:
        for table_name, id_column, _real_column, ns_column, _allow_negative in _FINANCIAL_NS_BACKFILL_CONFIG:
            cursor.execute(
                f"""
                SELECT {id_column}, {ns_column}, typeof({ns_column})
                FROM {table_name}
                WHERE {ns_column} IS NOT NULL AND typeof({ns_column}) != 'integer'
                """
            )
            for row_id, ns_actual, storage_type in cursor.fetchall():
                issues.append(
                    {
                        "table": table_name,
                        "row_id": str(row_id),
                        "column": ns_column,
                        "float_value": "",
                        "ns_expected": "",
                        "ns_actual": str(ns_actual),
                        "status": "nonsense",
                        "storage_type": storage_type,
                    }
                )
        return issues
    finally:
        _release_conn(conn)

def _ensure_registry_availability_column(cursor):
    if _is_postgres():
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_registry' AND column_name = 'x25519_public_key'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE agent_registry ADD COLUMN x25519_public_key TEXT")
    else:
        cursor.execute("PRAGMA table_info(agent_registry)")
        columns = [row[1] for row in cursor.fetchall()]
        if "x25519_public_key" not in columns:
            cursor.execute("ALTER TABLE agent_registry ADD COLUMN x25519_public_key TEXT")

    if _is_postgres():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_registry' AND column_name = 'availability'"
        )
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE agent_registry ADD COLUMN availability TEXT NOT NULL DEFAULT 'unknown'")
    else:
        cursor.execute("PRAGMA table_info(agent_registry)")
        columns = [row[1] for row in cursor.fetchall()]
        if "availability" not in columns:
            cursor.execute("ALTER TABLE agent_registry ADD COLUMN availability TEXT NOT NULL DEFAULT 'unknown'")

def _ensure_tasks_expires_in_seconds_column(cursor):
    if _is_postgres():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks' AND column_name = 'expires_in_seconds'"
        )
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE tasks ADD COLUMN expires_in_seconds INTEGER")
    else:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "expires_in_seconds" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN expires_in_seconds INTEGER")


def _ensure_tasks_envelope_json_column(cursor):
    if _is_postgres():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks' AND column_name = 'envelope_json'"
        )
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE tasks ADD COLUMN envelope_json TEXT")
    else:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "envelope_json" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN envelope_json TEXT")


def _ensure_tasks_verifier_type_column(cursor):
    if _is_postgres():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks' AND column_name = 'verifier_type'"
        )
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE tasks ADD COLUMN verifier_type TEXT")
    else:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "verifier_type" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN verifier_type TEXT")

def _ensure_tasks_rebroadcast_count_column(cursor):
    if _is_postgres():
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks' AND column_name = 'rebroadcast_count'"
        )
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE tasks ADD COLUMN rebroadcast_count INTEGER NOT NULL DEFAULT 0")
    else:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "rebroadcast_count" not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN rebroadcast_count INTEGER NOT NULL DEFAULT 0")

def init_db():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ledger (
            node_id TEXT PRIMARY KEY,
            pub_pem TEXT NOT NULL,
            balance REAL NOT NULL,
            balance_ns INTEGER
        )
    ''')
    _ensure_ledger_balance_ns_column(cursor)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_registrations (
            node_id TEXT PRIMARY KEY,
            pub_pem TEXT NOT NULL,
            created_at REAL NOT NULL,
            approved_at REAL,
            approved_by TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            provider_id TEXT,
            payload TEXT NOT NULL,
            bounty REAL NOT NULL,
            status TEXT NOT NULL,
            target_node TEXT,
            model_requirement TEXT,
            expires_in_seconds INTEGER,
            result_payload TEXT,
            payload_uri TEXT,
            result_uri TEXT,
            verifier_type TEXT,
            rebroadcast_count INTEGER NOT NULL DEFAULT 0,
            bounty_ns INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
    _ensure_tasks_expires_in_seconds_column(cursor)
    _ensure_tasks_envelope_json_column(cursor)
    _ensure_tasks_verifier_type_column(cursor)
    _ensure_tasks_rebroadcast_count_column(cursor)
    _ensure_tasks_bounty_ns_column(cursor)
    if not _is_postgres():
        try:
            cursor.execute("ALTER TABLE tasks ADD COLUMN payload_uri TEXT")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE tasks ADD COLUMN result_uri TEXT")
        except Exception:
            pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS idempotency (
            node_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            idem_key TEXT NOT NULL,
            response TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (node_id, endpoint, idem_key)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agent_registry (
            node_id TEXT PRIMARY KEY,
            alias TEXT,
            skills TEXT NOT NULL,
            models TEXT NOT NULL,
            metadata TEXT NOT NULL,
            availability TEXT NOT NULL DEFAULT 'unknown',
            updated_at REAL NOT NULL,
            x25519_public_key TEXT
        )
    ''')
    _ensure_registry_availability_column(cursor)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reputation (
            node_id TEXT PRIMARY KEY,
            score REAL NOT NULL,
            total_reviews INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_reviews (
            task_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS escrows (
            task_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            provider_id TEXT,
            amount REAL NOT NULL,
            amount_ns INTEGER,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
    _ensure_escrows_amount_ns_column(cursor)
    _assert_financial_ns_column_affinity(cursor)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS disputes (
            dispute_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            resolution TEXT,
            created_at REAL NOT NULL,
            resolved_at REAL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_dms (
            task_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            target_node TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    ''')
    # Index for efficient lookup of pending DMs by target node + ordering by creation time
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_pending_dms_target_created
        ON pending_dms (target_node, created_at)
    ''')
    conn.commit()
    _release_conn(conn)
    global FINANCIAL_NS_BACKFILL_REPORT
    FINANCIAL_NS_BACKFILL_REPORT = backfill_financial_ns_columns()
    FINANCIAL_NS_BACKFILL_REPORT.extend(validate_financial_ns_storage())

def register_node(node_id: str, pub_pem: str) -> dict:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT balance FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT balance FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    if row:
        _release_conn(conn)
        return {"status": "registered", "balance": row[0]}
    if _is_postgres():
        cursor.execute(
            "INSERT INTO pending_registrations (node_id, pub_pem, created_at) VALUES (%s, %s, %s) ON CONFLICT (node_id) DO NOTHING",
            (node_id, pub_pem, time.time())
        )
    else:
        cursor.execute(
            "INSERT OR IGNORE INTO pending_registrations (node_id, pub_pem, created_at) VALUES (?, ?, ?)",
            (node_id, pub_pem, time.time())
        )
    conn.commit()
    _release_conn(conn)
    return {"status": "pending", "balance": 0.0}

def approve_registration(node_id: str, approved_by: str, initial_balance: float = 10.0) -> bool:
    initial_balance_ns, initial_balance = _financial_pair(initial_balance, "balance", allow_negative=False)
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT pub_pem FROM pending_registrations WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT pub_pem FROM pending_registrations WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return False
    pub_pem = row[0]
    now = time.time()
    if _is_postgres():
        cursor.execute(
            """
            INSERT INTO ledger (node_id, pub_pem, balance, balance_ns)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (node_id) DO UPDATE SET
                balance = ledger.balance + EXCLUDED.balance,
                balance_ns = CASE
                    WHEN ledger.balance_ns IS NULL THEN NULL
                    ELSE ledger.balance_ns + EXCLUDED.balance_ns
                END
            """,
            (node_id, pub_pem, initial_balance, initial_balance_ns)
        )
        cursor.execute(
            "UPDATE pending_registrations SET approved_at = %s, approved_by = %s WHERE node_id = %s",
            (now, approved_by, node_id)
        )
    else:
        cursor.execute(
            """
            INSERT INTO ledger (node_id, pub_pem, balance, balance_ns)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                balance = balance + excluded.balance,
                balance_ns = CASE
                    WHEN balance_ns IS NULL THEN NULL
                    ELSE balance_ns + excluded.balance_ns
                END
            """,
            (node_id, pub_pem, initial_balance, initial_balance_ns)
        )
        cursor.execute(
            "UPDATE pending_registrations SET approved_at = ?, approved_by = ? WHERE node_id = ?",
            (now, approved_by, node_id)
        )
    conn.commit()
    _release_conn(conn)
    return True

def get_pending_registrations() -> list:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT node_id, pub_pem, created_at FROM pending_registrations WHERE approved_at IS NULL ORDER BY created_at DESC")
    else:
        cursor.execute("SELECT node_id, pub_pem, created_at FROM pending_registrations WHERE approved_at IS NULL ORDER BY created_at DESC")
    rows = cursor.fetchall()
    _release_conn(conn)
    return [{"node_id": row[0], "pub_pem": row[1], "created_at": row[2]} for row in rows]

def get_pub_pem(node_id: str) -> Optional[str]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT pub_pem FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT pub_pem FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    if row:
        _release_conn(conn)
        return row[0]
    # Check pending_registrations for nodes awaiting admin approval
    if _is_postgres():
        cursor.execute("SELECT pub_pem FROM pending_registrations WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT pub_pem FROM pending_registrations WHERE node_id = ?", (node_id,))
    pending_row = cursor.fetchone()
    _release_conn(conn)
    return pending_row[0] if pending_row else None

def get_balance(node_id: str) -> Optional[float]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT balance FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT balance FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    _release_conn(conn)
    return row[0] if row else None

# Public alias: the integer ns column is the canonical source of truth, and
# the money layer reads financial rows through this coercion helper.
coerce_ns_from_row = _coerce_ns_from_row

def get_balance_row(node_id: str) -> Optional[tuple]:
    """Return (balance, balance_ns) for a node, or None if it does not exist.

    Exposes the canonical integer-ns column alongside the legacy float so
    callers can treat ns as the source of truth and convert to float only at
    the response boundary.
    """
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT balance, balance_ns FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    _release_conn(conn)
    if not row:
        return None
    return (row[0], row[1])

def get_node_count() -> int:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT COUNT(*) FROM ledger")
    else:
        cursor.execute("SELECT COUNT(*) FROM ledger")
    row = cursor.fetchone()
    _release_conn(conn)
    return int(row[0]) if row else 0

def set_balance(node_id: str, balance: float):
    balance_ns, balance = _financial_pair(balance, "balance", allow_negative=False)
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("UPDATE ledger SET balance = %s, balance_ns = %s WHERE node_id = %s", (balance, balance_ns, node_id))
    else:
        cursor.execute("UPDATE ledger SET balance = ?, balance_ns = ? WHERE node_id = ?", (balance, balance_ns, node_id))
    conn.commit()
    _release_conn(conn)

def add_balance(node_id: str, amount: float):
    amount_ns, amount = _financial_pair(amount, "amount", allow_negative=True)
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance + %s,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance + %s) * 1000000000 AS BIGINT) ELSE balance_ns + %s END
            WHERE node_id = %s
            """,
            (amount, amount, amount_ns, node_id)
        )
    else:
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance + ?,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance + ?) * 1000000000 AS INTEGER) ELSE balance_ns + ? END
            WHERE node_id = ?
            """,
            (amount, amount, amount_ns, node_id)
        )
    conn.commit()
    _release_conn(conn)

def deduct_balance(node_id: str, amount: float) -> bool:
    amount_ns, amount = _financial_pair(amount, "amount", allow_negative=False)
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance - %s,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance - %s) * 1000000000 AS BIGINT) ELSE balance_ns - %s END
            WHERE node_id = %s AND balance >= %s AND (balance_ns IS NULL OR balance_ns >= %s)
            """,
            (amount, amount, amount_ns, node_id, amount, amount_ns)
        )
    else:
        cursor.execute(
            """
            UPDATE ledger
            SET balance = balance - ?,
                balance_ns = CASE WHEN balance_ns IS NULL THEN CAST((balance - ?) * 1000000000 AS INTEGER) ELSE balance_ns - ? END
            WHERE node_id = ? AND balance >= ? AND (balance_ns IS NULL OR balance_ns >= ?)
            """,
            (amount, amount, amount_ns, node_id, amount, amount_ns)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def create_task(
    task_id: str,
    consumer_id: str,
    payload: str,
    bounty: float,
    status: str,
    target_node: Optional[str],
    model_requirement: Optional[str],
    created_at: float,
    result_payload: Optional[str] = None,
    payload_uri: Optional[str] = None,
    expires_in_seconds: Optional[int] = None,
    verifier_type: Optional[str] = None,
):
    bounty_ns, bounty = _financial_pair(bounty, "bounty", allow_negative=True)
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "INSERT INTO tasks (task_id, consumer_id, provider_id, payload, bounty, bounty_ns, status, target_node, model_requirement, expires_in_seconds, result_payload, payload_uri, verifier_type, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (task_id, consumer_id, None, payload, bounty, bounty_ns, status, target_node, model_requirement, expires_in_seconds, result_payload, payload_uri, verifier_type, created_at, created_at)
        )
    else:
        cursor.execute(
            "INSERT INTO tasks (task_id, consumer_id, provider_id, payload, bounty, bounty_ns, status, target_node, model_requirement, expires_in_seconds, result_payload, payload_uri, verifier_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, consumer_id, None, payload, bounty, bounty_ns, status, target_node, model_requirement, expires_in_seconds, result_payload, payload_uri, verifier_type, created_at, created_at)
        )
    conn.commit()
    _release_conn(conn)

def update_task_assignment(task_id: str, provider_id: str, status: str, updated_at: float):
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET provider_id = %s, status = %s, updated_at = %s WHERE task_id = %s",
            (provider_id, status, updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET provider_id = ?, status = ?, updated_at = ? WHERE task_id = ?",
            (provider_id, status, updated_at, task_id)
        )
    conn.commit()
    _release_conn(conn)

def set_task_envelope(task_id: str, envelope_json: str):
    """Persist the full new_task envelope (JSON) for a queued DM so it can be
    replayed on reconnect with the exact same shape as live delivery."""
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET envelope_json = %s WHERE task_id = %s",
            (envelope_json, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET envelope_json = ? WHERE task_id = ?",
            (envelope_json, task_id)
        )
    conn.commit()
    _release_conn(conn)

def update_task_result(task_id: str, provider_id: str, result_payload: str, status: str, updated_at: float, result_uri: Optional[str] = None):
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET provider_id = %s, result_payload = %s, result_uri = %s, status = %s, updated_at = %s WHERE task_id = %s",
            (provider_id, result_payload, result_uri, status, updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET provider_id = ?, result_payload = ?, result_uri = ?, status = ?, updated_at = ? WHERE task_id = ?",
            (provider_id, result_payload, result_uri, status, updated_at, task_id)
        )
    conn.commit()
    _release_conn(conn)


def submit_task_result_for_verification(
    task_id: str,
    provider_id: str,
    result_payload: str,
    updated_at: float,
    result_uri: Optional[str] = None,
    idem_node_id: Optional[str] = None,
    idem_endpoint: Optional[str] = None,
    idem_key: Optional[str] = None,
    expected_status: str = "assigned",
) -> dict:
    """Store a provider result without releasing escrow until the verifier accepts."""
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        if not _is_postgres():
            cursor.execute("BEGIN IMMEDIATE")

        if idem_node_id and idem_endpoint and idem_key:
            if _is_postgres():
                cursor.execute(
                    "SELECT response, status_code FROM idempotency WHERE node_id = %s AND endpoint = %s AND idem_key = %s",
                    (idem_node_id, idem_endpoint, idem_key),
                )
            else:
                cursor.execute(
                    "SELECT response, status_code FROM idempotency WHERE node_id = ? AND endpoint = ? AND idem_key = ?",
                    (idem_node_id, idem_endpoint, idem_key),
                )
            existing = cursor.fetchone()
            if existing:
                conn.rollback()
                return {
                    "status": "idempotent",
                    "response": json.loads(existing[0]),
                    "status_code": existing[1],
                }

        if _is_postgres():
            cursor.execute(
                "SELECT provider_id, status, verifier_type FROM tasks WHERE task_id = %s FOR UPDATE",
                (task_id,),
            )
        else:
            cursor.execute(
                "SELECT provider_id, status, verifier_type FROM tasks WHERE task_id = ?",
                (task_id,),
            )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {"status": "not_found"}
        assigned_provider, task_status, verifier_type = row[0], row[1], row[2]
        if verifier_type not in ("manual_acceptance", "automated"):
            conn.rollback()
            return {"status": "not_verifier_gated"}
        if task_status == "submitted_result" and assigned_provider == provider_id:
            response_payload = {"status": "pending_verification", "task_id": task_id, "verifier_type": verifier_type}
            if idem_node_id and idem_endpoint and idem_key:
                payload = json.dumps(response_payload)
                if _is_postgres():
                    cursor.execute(
                        "INSERT INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (node_id, endpoint, idem_key) DO NOTHING",
                        (idem_node_id, idem_endpoint, idem_key, payload, 200, updated_at),
                    )
                else:
                    cursor.execute(
                        "INSERT OR IGNORE INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (idem_node_id, idem_endpoint, idem_key, payload, 200, updated_at),
                    )
            conn.commit()
            return {"status": "pending_verification", "response": response_payload}
        if task_status != expected_status:
            conn.rollback()
            return {"status": "not_active", "task_status": task_status}
        if assigned_provider != provider_id:
            conn.rollback()
            return {"status": "assigned_to_other", "assigned_provider": assigned_provider}

        if _is_postgres():
            cursor.execute(
                "UPDATE tasks SET result_payload = %s, result_uri = %s, status = %s, updated_at = %s WHERE task_id = %s AND status = %s AND provider_id = %s",
                (result_payload, result_uri, "submitted_result", updated_at, task_id, "assigned", provider_id),
            )
        else:
            cursor.execute(
                "UPDATE tasks SET result_payload = ?, result_uri = ?, status = ?, updated_at = ? WHERE task_id = ? AND status = ? AND provider_id = ?",
                (result_payload, result_uri, "submitted_result", updated_at, task_id, "assigned", provider_id),
            )
        if cursor.rowcount == 0:
            conn.rollback()
            return {"status": "not_active", "task_status": task_status}

        response_payload = {"status": "pending_verification", "task_id": task_id, "verifier_type": verifier_type}
        if idem_node_id and idem_endpoint and idem_key:
            payload = json.dumps(response_payload)
            if _is_postgres():
                cursor.execute(
                    "INSERT INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (node_id, endpoint, idem_key) DO NOTHING",
                    (idem_node_id, idem_endpoint, idem_key, payload, 200, updated_at),
                )
            else:
                cursor.execute(
                    "INSERT OR IGNORE INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (idem_node_id, idem_endpoint, idem_key, payload, 200, updated_at),
                )
        conn.commit()
        return {"status": "pending_verification", "response": response_payload}
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)

def reject_task_if_assigned(task_id: str, provider_id: str, updated_at: float) -> dict:
    """Reject an assigned task without settling payment to the provider."""
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        if not _is_postgres():
            cursor.execute("BEGIN IMMEDIATE")

        if _is_postgres():
            cursor.execute(
                "SELECT consumer_id, provider_id, bounty, status FROM tasks WHERE task_id = %s FOR UPDATE",
                (task_id,),
            )
        else:
            cursor.execute(
                "SELECT consumer_id, provider_id, bounty, status FROM tasks WHERE task_id = ?",
                (task_id,),
            )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {"status": "not_found"}

        consumer_id, assigned_provider, bounty, task_status = row[0], row[1], float(row[2]), row[3]
        if task_status == "rejected" and assigned_provider == provider_id:
            conn.commit()
            return {"status": "already_rejected", "ledger_events": []}
        if task_status != "assigned":
            conn.rollback()
            return {"status": "not_active", "task_status": task_status}
        if assigned_provider != provider_id:
            conn.rollback()
            return {"status": "assigned_to_other", "assigned_provider": assigned_provider}

        ledger_events: list[dict] = []
        if bounty != 0:
            if _is_postgres():
                cursor.execute(
                    "SELECT consumer_id, amount, status FROM escrows WHERE task_id = %s FOR UPDATE",
                    (task_id,),
                )
            else:
                cursor.execute(
                    "SELECT consumer_id, amount, status FROM escrows WHERE task_id = ?",
                    (task_id,),
                )
            escrow = cursor.fetchone()
            if bounty > 0 and (not escrow or escrow[2] != "held"):
                conn.rollback()
                return {"status": "escrow_not_held"}
            if escrow and escrow[2] == "held":
                escrow_consumer_id = escrow[0]
                amount = float(escrow[1])
                if _is_postgres():
                    cursor.execute(
                        "UPDATE escrows SET status = %s, updated_at = %s WHERE task_id = %s AND status = %s",
                        ("refunded", updated_at, task_id, "held"),
                    )
                    _cursor_add_balance(cursor, escrow_consumer_id, amount)
                else:
                    cursor.execute(
                        "UPDATE escrows SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                        ("refunded", updated_at, task_id, "held"),
                    )
                    _cursor_add_balance(cursor, escrow_consumer_id, amount)
                ledger_events.append({"kind": "TASK_REJECT_REFUND", "node_id": escrow_consumer_id, "amount": amount})

        if _is_postgres():
            cursor.execute(
                "UPDATE tasks SET status = %s, updated_at = %s WHERE task_id = %s AND status = %s AND provider_id = %s",
                ("rejected", updated_at, task_id, "assigned", provider_id),
            )
        else:
            cursor.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ? AND provider_id = ?",
                ("rejected", updated_at, task_id, "assigned", provider_id),
            )
        if cursor.rowcount == 0:
            conn.rollback()
            return {"status": "not_active"}

        conn.commit()
        return {
            "status": "success",
            "task": {
                "task_id": task_id,
                "consumer_id": consumer_id,
                "provider_id": provider_id,
                "bounty": bounty,
                "status": "rejected",
            },
            "ledger_events": ledger_events,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)

def update_task_status(task_id: str, status: str, updated_at: float):
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = %s, provider_id = NULL, updated_at = %s WHERE task_id = %s",
            (status, updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = ?, provider_id = NULL, updated_at = ? WHERE task_id = ?",
            (status, updated_at, task_id)
        )
    conn.commit()
    _release_conn(conn)

def assign_task_if_open(task_id: str, provider_id: str, updated_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET provider_id = %s, status = 'assigned', updated_at = %s WHERE task_id = %s AND status = 'bidding' AND provider_id IS NULL",
            (provider_id, updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET provider_id = ?, status = 'assigned', updated_at = ? WHERE task_id = ? AND status = 'bidding' AND provider_id IS NULL",
            (provider_id, updated_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def assign_data_task_with_escrow_if_open(task_id: str, provider_id: str, cost: float, updated_at: float) -> dict:
    """Atomically assign a data-market task and escrow the buyer's SECONDS.

    Data-market payloads are revealed to the provider in the bid response, so the
    buyer must be charged/escrowed before the route returns secret_data.
    """
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        if not _is_postgres():
            cursor.execute("BEGIN IMMEDIATE")
        if _is_postgres():
            cursor.execute(
                "SELECT status, provider_id, bounty FROM tasks WHERE task_id = %s FOR UPDATE",
                (task_id,),
            )
        else:
            cursor.execute(
                "SELECT status, provider_id, bounty FROM tasks WHERE task_id = ?",
                (task_id,),
            )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {"status": "not_found"}
        status, assigned_provider, bounty = row[0], row[1], float(row[2])
        if status != "bidding" or assigned_provider is not None:
            conn.rollback()
            return {"status": "already_assigned"}
        if bounty >= 0:
            conn.rollback()
            return {"status": "not_data_market"}

        if _cursor_deduct_balance(cursor, provider_id, cost) == 0:
            conn.rollback()
            return {"status": "insufficient_balance"}

        if _is_postgres():
            _cursor_insert_held_escrow(cursor, task_id, provider_id, None, cost, updated_at, updated_at)
            cursor.execute(
                "UPDATE tasks SET provider_id = %s, status = 'assigned', updated_at = %s WHERE task_id = %s AND status = 'bidding' AND provider_id IS NULL",
                (provider_id, updated_at, task_id),
            )
        else:
            _cursor_insert_held_escrow(cursor, task_id, provider_id, None, cost, updated_at, updated_at)
            cursor.execute(
                "UPDATE tasks SET provider_id = ?, status = 'assigned', updated_at = ? WHERE task_id = ? AND status = 'bidding' AND provider_id IS NULL",
                (provider_id, updated_at, task_id),
            )
        if cursor.rowcount == 0:
            conn.rollback()
            return {"status": "already_assigned"}
        conn.commit()
        return {"status": "accepted"}
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)

def cancel_task_if_open(task_id: str, updated_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = 'cancelled', provider_id = NULL, updated_at = %s WHERE task_id = %s AND status = 'bidding' AND provider_id IS NULL",
            (updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = 'cancelled', provider_id = NULL, updated_at = ? WHERE task_id = ? AND status = 'bidding' AND provider_id IS NULL",
            (updated_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def get_task(task_id: str) -> Optional[dict]:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return None
    if _is_postgres():
        result = _row_to_dict(cursor, row)
        _release_conn(conn)
        return result
    result = dict(row)
    _release_conn(conn)
    return result

def get_active_tasks() -> list:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM tasks WHERE status IN ('bidding', 'assigned')")
    else:
        cursor.execute("SELECT * FROM tasks WHERE status IN ('bidding', 'assigned')")
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
        _release_conn(conn)
        return result
    result = [dict(row) for row in rows]
    _release_conn(conn)
    return result

def get_queued_dms_for_node(node_id: str) -> list:
    """Return store-and-forward DMs queued for node_id while it was offline, oldest first."""
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "SELECT * FROM tasks WHERE target_node = %s AND status = 'queued_dm' ORDER BY created_at ASC",
            (node_id,),
        )
    else:
        cursor.execute(
            "SELECT * FROM tasks WHERE target_node = ? AND status = 'queued_dm' ORDER BY created_at ASC",
            (node_id,),
        )
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result


def get_pending_tasks_for_provider(node_id: str) -> list:
    """Return assigned tasks for node_id that still need provider-side processing."""
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "SELECT * FROM tasks WHERE provider_id = %s AND status = 'assigned' ORDER BY created_at ASC",
            (node_id,),
        )
    else:
        cursor.execute(
            "SELECT * FROM tasks WHERE provider_id = ? AND status = 'assigned' ORDER BY created_at ASC",
            (node_id,),
        )
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result


def count_queued_dms_for_node(node_id: str) -> int:
    """Count store-and-forward DMs currently queued for node_id."""
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "SELECT COUNT(*) FROM tasks WHERE target_node = %s AND status = 'queued_dm'",
            (node_id,),
        )
    else:
        cursor.execute(
            "SELECT COUNT(*) FROM tasks WHERE target_node = ? AND status = 'queued_dm'",
            (node_id,),
        )
    row = cursor.fetchone()
    _release_conn(conn)
    return int(row[0]) if row else 0


def get_assigned_tasks_before(cutoff_ts: float) -> list:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM tasks WHERE status = 'assigned' AND updated_at < %s", (cutoff_ts,))
    else:
        cursor.execute("SELECT * FROM tasks WHERE status = 'assigned' AND updated_at < ?", (cutoff_ts,))
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result

def expire_task_if_assigned(task_id: str, updated_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = 'expired', provider_id = NULL, updated_at = %s WHERE task_id = %s AND status = 'assigned'",
            (updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = 'expired', provider_id = NULL, updated_at = ? WHERE task_id = ? AND status = 'assigned'",
            (updated_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def expire_task_if_submitted_result(task_id: str, updated_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = 'expired', updated_at = %s WHERE task_id = %s AND status = 'submitted_result'",
            (updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = 'expired', updated_at = ? WHERE task_id = ? AND status = 'submitted_result'",
            (updated_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def expire_task_if_bidding(task_id: str, updated_at: float) -> bool:
    """Expire a bidding task (no provider accepted)"""
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = 'expired', provider_id = NULL, updated_at = %s WHERE task_id = %s AND status = 'bidding'",
            (updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = 'expired', provider_id = NULL, updated_at = ? WHERE task_id = ? AND status = 'bidding'",
            (updated_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def get_bidding_tasks_before(cutoff_ts: float) -> list:
    """Get bidding tasks older than cutoff - for timeout sweep"""
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT task_id, consumer_id, payload, bounty, updated_at FROM tasks WHERE status = 'bidding' AND updated_at < %s", (cutoff_ts,))
    else:
        cursor.execute("SELECT task_id, consumer_id, payload, bounty, updated_at FROM tasks WHERE status = 'bidding' AND updated_at < ?", (cutoff_ts,))
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result

def get_assigned_tasks_for_timeout(now_ts: float, default_timeout: int, min_timeout: int, max_timeout: int) -> list:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'assigned'
              AND updated_at < (
                %s - LEAST(GREATEST(COALESCE(expires_in_seconds, %s), %s), %s)
              )
            """,
            (now_ts, default_timeout, min_timeout, max_timeout),
        )
    else:
        cursor.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'assigned'
              AND updated_at < (
                ? - MIN(MAX(COALESCE(expires_in_seconds, ?), ?), ?)
              )
            """,
            (now_ts, default_timeout, min_timeout, max_timeout),
        )
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result

def get_submitted_result_tasks_for_timeout(now_ts: float, submitted_result_timeout: int) -> list:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'submitted_result'
              AND updated_at < (%s - %s)
            """,
            (now_ts, submitted_result_timeout),
        )
    else:
        cursor.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'submitted_result'
              AND updated_at < (? - ?)
            """,
            (now_ts, submitted_result_timeout),
        )
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result

def get_bidding_tasks_for_timeout(now_ts: float, default_timeout: int, min_timeout: int, max_timeout: int) -> list:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            """
            SELECT task_id, consumer_id, payload, bounty, updated_at, expires_in_seconds
            FROM tasks
            WHERE status = 'bidding'
              AND updated_at < (
                %s - LEAST(GREATEST(COALESCE(expires_in_seconds, %s), %s), %s)
              )
            """,
            (now_ts, default_timeout, min_timeout, max_timeout),
        )
    else:
        cursor.execute(
            """
            SELECT task_id, consumer_id, payload, bounty, updated_at, expires_in_seconds
            FROM tasks
            WHERE status = 'bidding'
              AND updated_at < (
                ? - MIN(MAX(COALESCE(expires_in_seconds, ?), ?), ?)
              )
            """,
            (now_ts, default_timeout, min_timeout, max_timeout),
        )
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    return result

def requeue_task_if_assigned(task_id: str, updated_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = 'bidding', provider_id = NULL, rebroadcast_count = COALESCE(rebroadcast_count, 0) + 1, updated_at = %s WHERE task_id = %s AND status = 'assigned'",
            (updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = 'bidding', provider_id = NULL, rebroadcast_count = COALESCE(rebroadcast_count, 0) + 1, updated_at = ? WHERE task_id = ? AND status = 'assigned'",
            (updated_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def get_last_completed_task_time() -> Optional[float]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT updated_at FROM tasks WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 1")
    else:
        cursor.execute("SELECT updated_at FROM tasks WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 1")
    row = cursor.fetchone()
    _release_conn(conn)
    return float(row[0]) if row else None

def check_database_health() -> dict:
    conn = None
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        if _is_postgres():
            cursor.execute("SELECT 1")
        else:
            cursor.execute("SELECT 1")
        row = cursor.fetchone()
        ok = bool(row and row[0] == 1)
        return {"ok": ok, "backend": "postgres" if _is_postgres() else "sqlite"}
    except Exception as exc:
        return {"ok": False, "backend": "postgres" if _is_postgres() else "sqlite", "error": str(exc)}
    finally:
        if conn is not None:
            _release_conn(conn)

def get_idempotency(node_id: str, endpoint: str, idem_key: str) -> Optional[dict]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "SELECT response, status_code FROM idempotency WHERE node_id = %s AND endpoint = %s AND idem_key = %s",
            (node_id, endpoint, idem_key)
        )
    else:
        cursor.execute(
            "SELECT response, status_code FROM idempotency WHERE node_id = ? AND endpoint = ? AND idem_key = ?",
            (node_id, endpoint, idem_key)
        )
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return None
    response = json.loads(row[0])
    result = {"response": response, "status_code": row[1]}
    _release_conn(conn)
    return result

def set_idempotency(node_id: str, endpoint: str, idem_key: str, response: dict, status_code: int, created_at: float):
    conn = _get_conn()
    cursor = conn.cursor()
    payload = json.dumps(response)
    if _is_postgres():
        cursor.execute(
            "INSERT INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (node_id, endpoint, idem_key) DO NOTHING",
            (node_id, endpoint, idem_key, payload, status_code, created_at)
        )
    else:
        cursor.execute(
            "INSERT OR IGNORE INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, endpoint, idem_key, payload, status_code, created_at)
        )
    conn.commit()
    _release_conn(conn)

def complete_task_atomic(
    task_id: str,
    provider_id: str,
    result_payload: str,
    updated_at: float,
    result_uri: Optional[str] = None,
    idem_node_id: Optional[str] = None,
    idem_endpoint: Optional[str] = None,
    idem_key: Optional[str] = None,
    expected_status: str = "assigned",
) -> dict:
    """Complete a task and settle its bounty in one database transaction."""
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        if not _is_postgres():
            cursor.execute("BEGIN IMMEDIATE")

        if idem_node_id and idem_endpoint and idem_key:
            if _is_postgres():
                cursor.execute(
                    "SELECT response, status_code FROM idempotency WHERE node_id = %s AND endpoint = %s AND idem_key = %s",
                    (idem_node_id, idem_endpoint, idem_key),
                )
            else:
                cursor.execute(
                    "SELECT response, status_code FROM idempotency WHERE node_id = ? AND endpoint = ? AND idem_key = ?",
                    (idem_node_id, idem_endpoint, idem_key),
                )
            existing = cursor.fetchone()
            if existing:
                conn.rollback()
                return {
                    "status": "idempotent",
                    "response": json.loads(existing[0]),
                    "status_code": existing[1],
                }

        if _is_postgres():
            cursor.execute(
                """
                SELECT task_id, consumer_id, provider_id, payload, bounty, status,
                       target_node, model_requirement, expires_in_seconds, result_payload,
                       payload_uri, result_uri, verifier_type, created_at, updated_at
                FROM tasks
                WHERE task_id = %s
                FOR UPDATE
                """,
                (task_id,),
            )
        else:
            cursor.execute(
                """
                SELECT task_id, consumer_id, provider_id, payload, bounty, status,
                       target_node, model_requirement, expires_in_seconds, result_payload,
                       payload_uri, result_uri, verifier_type, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {"status": "not_found"}

        columns = [
            "task_id", "consumer_id", "provider_id", "payload", "bounty", "status",
            "target_node", "model_requirement", "expires_in_seconds", "result_payload",
            "payload_uri", "result_uri", "verifier_type", "created_at", "updated_at",
        ]
        task = dict(zip(columns, row))
        task_status = task["status"]
        assigned_provider = task.get("provider_id")
        if task_status != expected_status:
            conn.rollback()
            return {"status": "not_active", "task_status": task_status}
        if assigned_provider != provider_id:
            conn.rollback()
            return {"status": "assigned_to_other", "assigned_provider": assigned_provider}

        bounty = float(task["bounty"])
        ledger_events: list[dict] = []

        if bounty > 0:
            if _is_postgres():
                cursor.execute(
                    "SELECT amount, status FROM escrows WHERE task_id = %s FOR UPDATE",
                    (task_id,),
                )
            else:
                cursor.execute(
                    "SELECT amount, status FROM escrows WHERE task_id = ?",
                    (task_id,),
                )
            escrow = cursor.fetchone()
            if not escrow or escrow[1] != "held":
                conn.rollback()
                return {"status": "escrow_not_held"}
            amount = float(escrow[0])
            if _is_postgres():
                cursor.execute(
                    "UPDATE escrows SET provider_id = %s, status = %s, updated_at = %s WHERE task_id = %s AND status = %s",
                    (provider_id, "released", updated_at, task_id, "held"),
                )
                _cursor_add_balance(cursor, provider_id, amount)
            else:
                cursor.execute(
                    "UPDATE escrows SET provider_id = ?, status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                    (provider_id, "released", updated_at, task_id, "held"),
                )
                _cursor_add_balance(cursor, provider_id, amount)
            ledger_events.append({"kind": "ESCROW_RELEASE", "node_id": provider_id, "amount": amount})
        elif bounty < 0:
            cost = abs(bounty)
            if _is_postgres():
                cursor.execute(
                    "SELECT consumer_id, amount, status FROM escrows WHERE task_id = %s FOR UPDATE",
                    (task_id,),
                )
            else:
                cursor.execute(
                    "SELECT consumer_id, amount, status FROM escrows WHERE task_id = ?",
                    (task_id,),
                )
            escrow = cursor.fetchone()
            if escrow and escrow[2] == "held":
                amount = float(escrow[1])
                if _is_postgres():
                    cursor.execute(
                        "UPDATE escrows SET provider_id = %s, status = %s, updated_at = %s WHERE task_id = %s AND status = %s",
                        (provider_id, "released", updated_at, task_id, "held"),
                    )
                    _cursor_add_balance(cursor, task["consumer_id"], amount)
                else:
                    cursor.execute(
                        "UPDATE escrows SET provider_id = ?, status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                        (provider_id, "released", updated_at, task_id, "held"),
                    )
                    _cursor_add_balance(cursor, task["consumer_id"], amount)
                ledger_events.append({"kind": "SELL_DATA", "node_id": task["consumer_id"], "amount": amount})
            else:
                # Compatibility path for data tasks assigned before bid-time escrow existed.
                if _cursor_deduct_balance(cursor, provider_id, cost) == 0:
                    conn.rollback()
                    return {"status": "insufficient_balance"}
                _cursor_add_balance(cursor, task["consumer_id"], cost)
                ledger_events.append({"kind": "BUY_DATA", "node_id": provider_id, "amount": -cost})
                ledger_events.append({"kind": "SELL_DATA", "node_id": task["consumer_id"], "amount": cost})

        if _is_postgres():
            cursor.execute(
                "UPDATE tasks SET provider_id = %s, result_payload = %s, result_uri = %s, status = %s, updated_at = %s WHERE task_id = %s AND status = %s",
                (provider_id, result_payload, result_uri, "completed", updated_at, task_id, expected_status),
            )
            cursor.execute("SELECT balance FROM ledger WHERE node_id = %s", (provider_id,))
        else:
            cursor.execute(
                "UPDATE tasks SET provider_id = ?, result_payload = ?, result_uri = ?, status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
                (provider_id, result_payload, result_uri, "completed", updated_at, task_id, expected_status),
            )
            cursor.execute("SELECT balance FROM ledger WHERE node_id = ?", (provider_id,))
        provider_balance_row = cursor.fetchone()
        provider_balance = float(provider_balance_row[0]) if provider_balance_row else 0.0

        task["status"] = "completed"
        task["provider_id"] = provider_id
        task["result_payload"] = result_payload
        task["result_uri"] = result_uri
        task["updated_at"] = updated_at

        response_payload = {"status": "success", "earned": bounty, "new_balance": provider_balance}
        if idem_node_id and idem_endpoint and idem_key:
            payload = json.dumps(response_payload)
            if _is_postgres():
                cursor.execute(
                    "INSERT INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (node_id, endpoint, idem_key) DO NOTHING",
                    (idem_node_id, idem_endpoint, idem_key, payload, 200, updated_at),
                )
            else:
                cursor.execute(
                    "INSERT OR IGNORE INTO idempotency (node_id, endpoint, idem_key, response, status_code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (idem_node_id, idem_endpoint, idem_key, payload, 200, updated_at),
                )

        conn.commit()
        return {
            "status": "success",
            "response": response_payload,
            "task": task,
            "ledger_events": ledger_events,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn)

def delete_idempotency_before(cutoff_ts: float) -> int:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("DELETE FROM idempotency WHERE created_at < %s", (cutoff_ts,))
    else:
        cursor.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff_ts,))
    deleted = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return int(deleted or 0)

def upsert_registry(node_id: str, alias: Optional[str], skills: list[str], models: list[str], metadata: dict, availability: str, updated_at: float, x25519_public_key: Optional[str] = None):
    conn = _get_conn()
    cursor = conn.cursor()
    _ensure_registry_availability_column(cursor)
    skills_payload = json.dumps(skills)
    models_payload = json.dumps(models)
    metadata_payload = json.dumps(metadata)
    
    if _is_postgres():
        query = """
            INSERT INTO agent_registry (node_id, alias, skills, models, metadata, availability, updated_at, x25519_public_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (node_id) DO UPDATE SET
                alias = EXCLUDED.alias,
                skills = EXCLUDED.skills,
                models = EXCLUDED.models,
                metadata = EXCLUDED.metadata,
                availability = EXCLUDED.availability,
                updated_at = EXCLUDED.updated_at
        """
        params = [node_id, alias, skills_payload, models_payload, metadata_payload, availability, updated_at, x25519_public_key]
        if x25519_public_key:
            query += ", x25519_public_key = EXCLUDED.x25519_public_key"
        cursor.execute(query, tuple(params))
    else:
        query = """
            INSERT INTO agent_registry (node_id, alias, skills, models, metadata, availability, updated_at, x25519_public_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                alias=excluded.alias,
                skills=excluded.skills,
                models=excluded.models,
                metadata=excluded.metadata,
                availability=excluded.availability,
                updated_at=excluded.updated_at
        """
        params = [node_id, alias, skills_payload, models_payload, metadata_payload, availability, updated_at, x25519_public_key]
        if x25519_public_key:
            query += ", x25519_public_key=excluded.x25519_public_key"
        cursor.execute(query, tuple(params))
        
    conn.commit()
    _release_conn(conn)

def update_registry_availability(node_id: str, availability: str, updated_at: float):
    conn = _get_conn()
    cursor = conn.cursor()
    _ensure_registry_availability_column(cursor)
    
    if _is_postgres():
        # Check if record exists first
        cursor.execute("SELECT 1 FROM agent_registry WHERE node_id = %s", (node_id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE agent_registry SET availability = %s, updated_at = %s WHERE node_id = %s",
                (availability, updated_at, node_id)
            )
        else:
            # If not exists, insert with default empty values
            cursor.execute(
                "INSERT INTO agent_registry (node_id, alias, skills, models, metadata, availability, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (node_id, None, '[]', '[]', '{}', availability, updated_at)
            )
    else:
        # Check if record exists first
        cursor.execute("SELECT 1 FROM agent_registry WHERE node_id = ?", (node_id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE agent_registry SET availability = ?, updated_at = ? WHERE node_id = ?",
                (availability, updated_at, node_id)
            )
        else:
            # If not exists, insert with default empty values
            cursor.execute(
                "INSERT INTO agent_registry (node_id, alias, skills, models, metadata, availability, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (node_id, None, '[]', '[]', '{}', availability, updated_at)
            )
            
    conn.commit()
    _release_conn(conn)

def get_registry(node_id: str) -> Optional[dict]:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM agent_registry WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT * FROM agent_registry WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return None
    if _is_postgres():
        result = _row_to_dict(cursor, row)
    else:
        result = dict(row)
    _release_conn(conn)
    result["skills"] = json.loads(result["skills"]) if result.get("skills") else []
    result["models"] = json.loads(result["models"]) if result.get("models") else []
    result["metadata"] = json.loads(result["metadata"]) if result.get("metadata") else {}
    return result

def search_registry(alias: Optional[str], skill: Optional[str], model: Optional[str], availability: Optional[str], min_score: Optional[float], min_reviews: Optional[int], min_updated_at: Optional[float], limit: int) -> list[dict]:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    conditions = []
    params: list = []
    if min_score is not None:
        conditions.append("COALESCE(reputation.score, 0) >= %s" if _is_postgres() else "COALESCE(reputation.score, 0) >= ?")
        params.append(min_score)
    if min_reviews is not None:
        conditions.append("COALESCE(reputation.total_reviews, 0) >= %s" if _is_postgres() else "COALESCE(reputation.total_reviews, 0) >= ?")
        params.append(min_reviews)
    if availability:
        conditions.append("agent_registry.availability = %s" if _is_postgres() else "agent_registry.availability = ?")
        params.append(availability)
    if min_updated_at is not None:
        conditions.append("agent_registry.updated_at >= %s" if _is_postgres() else "agent_registry.updated_at >= ?")
        params.append(min_updated_at)
    if alias:
        if _is_postgres():
            conditions.append("agent_registry.alias ILIKE %s")
            params.append(f"%{alias}%")
        else:
            conditions.append("agent_registry.alias LIKE ?")
            params.append(f"%{alias}%")
    if skill:
        if _is_postgres():
            conditions.append("skills ILIKE %s")
            params.append(f"%{skill.lower()}%")
        else:
            conditions.append("skills LIKE ?")
            params.append(f"%{skill.lower()}%")
    if model:
        if _is_postgres():
            conditions.append("models ILIKE %s")
            params.append(f"%{model.lower()}%")
        else:
            conditions.append("models LIKE ?")
            params.append(f"%{model.lower()}%")
    where_clause = " AND ".join(conditions)
    query = "SELECT agent_registry.* FROM agent_registry LEFT JOIN reputation ON reputation.node_id = agent_registry.node_id"
    if where_clause:
        query += f" WHERE {where_clause}"
    query += " ORDER BY updated_at DESC LIMIT %s" if _is_postgres() else " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    if _is_postgres():
        result = [_row_to_dict(cursor, row) for row in rows]
    else:
        result = [dict(row) for row in rows]
    _release_conn(conn)
    for item in result:
        item["skills"] = json.loads(item["skills"]) if item.get("skills") else []
        item["models"] = json.loads(item["models"]) if item.get("models") else []
        item["metadata"] = json.loads(item["metadata"]) if item.get("metadata") else {}
    return result

def get_reputation(node_id: str) -> Optional[dict]:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM reputation WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT * FROM reputation WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return None
    if _is_postgres():
        result = _row_to_dict(cursor, row)
    else:
        result = dict(row)
    _release_conn(conn)
    return result

def submit_review(task_id: str, consumer_id: str, provider_id: str, rating: int, created_at: float) -> dict:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT task_id FROM task_reviews WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT task_id FROM task_reviews WHERE task_id = ?", (task_id,))
    if cursor.fetchone():
        _release_conn(conn)
        return {"status": "exists"}

    if _is_postgres():
        cursor.execute(
            "INSERT INTO task_reviews (task_id, consumer_id, provider_id, rating, created_at) VALUES (%s, %s, %s, %s, %s)",
            (task_id, consumer_id, provider_id, rating, created_at)
        )
        cursor.execute("SELECT score, total_reviews FROM reputation WHERE node_id = %s", (provider_id,))
    else:
        cursor.execute(
            "INSERT INTO task_reviews (task_id, consumer_id, provider_id, rating, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, consumer_id, provider_id, rating, created_at)
        )
        cursor.execute("SELECT score, total_reviews FROM reputation WHERE node_id = ?", (provider_id,))
    row = cursor.fetchone()
    if row:
        current_score = float(row[0])
        total_reviews = int(row[1])
    else:
        current_score = 0.0
        total_reviews = 0
    new_total = total_reviews + 1
    new_score = (current_score * total_reviews + rating) / new_total
    if _is_postgres():
        cursor.execute(
            "INSERT INTO reputation (node_id, score, total_reviews, updated_at) VALUES (%s, %s, %s, %s) ON CONFLICT (node_id) DO UPDATE SET score = EXCLUDED.score, total_reviews = EXCLUDED.total_reviews, updated_at = EXCLUDED.updated_at",
            (provider_id, new_score, new_total, created_at)
        )
    else:
        cursor.execute(
            "INSERT INTO reputation (node_id, score, total_reviews, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET score=excluded.score, total_reviews=excluded.total_reviews, updated_at=excluded.updated_at",
            (provider_id, new_score, new_total, created_at)
        )
    conn.commit()
    _release_conn(conn)
    return {"status": "success", "score": new_score, "total_reviews": new_total}

def create_escrow(task_id: str, consumer_id: str, amount: float, created_at: float):
    conn = _get_conn()
    cursor = conn.cursor()
    _cursor_insert_held_escrow(cursor, task_id, consumer_id, None, amount, created_at, created_at)
    conn.commit()
    _release_conn(conn)

def get_escrow(task_id: str) -> Optional[dict]:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM escrows WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT * FROM escrows WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return None
    if _is_postgres():
        result = _row_to_dict(cursor, row)
    else:
        result = dict(row)
    _release_conn(conn)
    return result

def release_escrow(task_id: str, provider_id: str, updated_at: float) -> Optional[float]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT amount, status FROM escrows WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT amount, status FROM escrows WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    if not row or row[1] != "held":
        _release_conn(conn)
        return None
    amount = float(row[0])
    if _is_postgres():
        cursor.execute(
            "UPDATE escrows SET provider_id = %s, status = %s, updated_at = %s WHERE task_id = %s",
            (provider_id, "released", updated_at, task_id)
        )
        _cursor_add_balance(cursor, provider_id, amount)
    else:
        cursor.execute(
            "UPDATE escrows SET provider_id = ?, status = ?, updated_at = ? WHERE task_id = ?",
            (provider_id, "released", updated_at, task_id)
        )
        _cursor_add_balance(cursor, provider_id, amount)
    conn.commit()
    _release_conn(conn)
    return amount

def refund_escrow(task_id: str, updated_at: float) -> Optional[float]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT consumer_id, amount, status FROM escrows WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT consumer_id, amount, status FROM escrows WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    if not row or row[2] != "held":
        _release_conn(conn)
        return None
    consumer_id = row[0]
    amount = float(row[1])
    if _is_postgres():
        cursor.execute(
            "UPDATE escrows SET status = %s, updated_at = %s WHERE task_id = %s",
            ("refunded", updated_at, task_id)
        )
        _cursor_add_balance(cursor, consumer_id, amount)
    else:
        cursor.execute(
            "UPDATE escrows SET status = ?, updated_at = ? WHERE task_id = ?",
            ("refunded", updated_at, task_id)
        )
        _cursor_add_balance(cursor, consumer_id, amount)
    conn.commit()
    _release_conn(conn)
    return amount

def chargeback_escrow(task_id: str, updated_at: float) -> dict:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT consumer_id, provider_id, amount, status FROM escrows WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT consumer_id, provider_id, amount, status FROM escrows WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    if not row or row[3] != "released":
        _release_conn(conn)
        return {"status": "invalid"}
    consumer_id = row[0]
    provider_id = row[1]
    amount = float(row[2])
    if _cursor_deduct_balance(cursor, provider_id, amount) == 0:
        _release_conn(conn)
        return {"status": "insufficient"}
    if _is_postgres():
        cursor.execute(
            "UPDATE escrows SET status = %s, updated_at = %s WHERE task_id = %s",
            ("chargeback", updated_at, task_id)
        )
        _cursor_add_balance(cursor, consumer_id, amount)
    else:
        cursor.execute(
            "UPDATE escrows SET status = ?, updated_at = ? WHERE task_id = ?",
            ("chargeback", updated_at, task_id)
        )
        _cursor_add_balance(cursor, consumer_id, amount)
    conn.commit()
    _release_conn(conn)
    return {"status": "success", "amount": amount, "consumer_id": consumer_id, "provider_id": provider_id}

def open_dispute(task_id: str, consumer_id: str, provider_id: str, reason: str, created_at: float) -> str:
    dispute_id = f"dispute_{task_id}"
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT dispute_id FROM disputes WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT dispute_id FROM disputes WHERE task_id = ?", (task_id,))
    if cursor.fetchone():
        _release_conn(conn)
        return "exists"
    if _is_postgres():
        cursor.execute(
            "INSERT INTO disputes (dispute_id, task_id, consumer_id, provider_id, status, reason, resolution, created_at, resolved_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (dispute_id, task_id, consumer_id, provider_id, "open", reason, None, created_at, None)
        )
    else:
        cursor.execute(
            "INSERT INTO disputes (dispute_id, task_id, consumer_id, provider_id, status, reason, resolution, created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, task_id, consumer_id, provider_id, "open", reason, None, created_at, None)
        )
    conn.commit()
    _release_conn(conn)
    return dispute_id

def get_dispute(task_id: str) -> Optional[dict]:
    conn = _get_conn()
    if not _is_postgres():
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT * FROM disputes WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("SELECT * FROM disputes WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        _release_conn(conn)
        return None
    if _is_postgres():
        result = _row_to_dict(cursor, row)
    else:
        result = dict(row)
    _release_conn(conn)
    return result

def resolve_dispute(task_id: str, resolution: str, resolved_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE disputes SET status = %s, resolution = %s, resolved_at = %s WHERE task_id = %s AND status = 'open'",
            ("resolved", resolution, resolved_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE disputes SET status = ?, resolution = ?, resolved_at = ? WHERE task_id = ? AND status = 'open'",
            ("resolved", resolution, resolved_at, task_id)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def queue_offline_dm(task_id: str, consumer_id: str, target_node: str, payload: str, created_at: float) -> None:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "INSERT INTO pending_dms (task_id, consumer_id, target_node, payload, created_at) VALUES (%s, %s, %s, %s, %s)",
            (task_id, consumer_id, target_node, payload, created_at)
        )
    else:
        cursor.execute(
            "INSERT INTO pending_dms (task_id, consumer_id, target_node, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, consumer_id, target_node, payload, created_at)
        )
    conn.commit()
    _release_conn(conn)

def get_pending_dms(target_node: str) -> list:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "SELECT task_id, consumer_id, target_node, payload, created_at FROM pending_dms WHERE target_node = %s ORDER BY created_at ASC",
            (target_node,)
        )
    else:
        cursor.execute(
            "SELECT task_id, consumer_id, target_node, payload, created_at FROM pending_dms WHERE target_node = ? ORDER BY created_at ASC",
            (target_node,)
        )
    rows = cursor.fetchall()
    _release_conn(conn)
    return [_row_to_dict(cursor, row) for row in rows]

def delete_pending_dm(task_id: str) -> None:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("DELETE FROM pending_dms WHERE task_id = %s", (task_id,))
    else:
        cursor.execute("DELETE FROM pending_dms WHERE task_id = ?", (task_id,))
    conn.commit()
    _release_conn(conn)

PENDING_DM_TTL_SECONDS = float(os.getenv("MEP_PENDING_DM_TTL_SECONDS", "86400"))  # Default 24h

def cleanup_expired_pending_dms() -> int:
    """Remove pending DMs older than PENDING_DM_TTL_SECONDS. Returns count of removed entries."""
    if PENDING_DM_TTL_SECONDS <= 0:
        return 0
    conn = _get_conn()
    cursor = conn.cursor()
    cutoff = time.time() - PENDING_DM_TTL_SECONDS
    if _is_postgres():
        cursor.execute("DELETE FROM pending_dms WHERE created_at < %s", (cutoff,))
    else:
        cursor.execute("DELETE FROM pending_dms WHERE created_at < ?", (cutoff,))
    removed = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return removed

init_db()
