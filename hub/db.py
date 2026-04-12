import sqlite3
import os
import json
import time
from typing import Optional

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

def init_db():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ledger (
            node_id TEXT PRIMARY KEY,
            pub_pem TEXT NOT NULL,
            balance REAL NOT NULL
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
            result_payload TEXT,
            payload_uri TEXT,
            result_uri TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
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
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
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

def register_node(node_id: str, pub_pem: str) -> float:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT balance FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT balance FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    if not row:
        if _is_postgres():
            cursor.execute(
                "INSERT INTO ledger (node_id, pub_pem, balance) VALUES (%s, %s, %s) ON CONFLICT (node_id) DO NOTHING",
                (node_id, pub_pem, 10.0)
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO ledger (node_id, pub_pem, balance) VALUES (?, ?, ?)",
                (node_id, pub_pem, 10.0)
            )
        conn.commit()
    if _is_postgres():
        cursor.execute("SELECT balance FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT balance FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    _release_conn(conn)
    return row[0] if row else 10.0

def get_pub_pem(node_id: str) -> Optional[str]:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("SELECT pub_pem FROM ledger WHERE node_id = %s", (node_id,))
    else:
        cursor.execute("SELECT pub_pem FROM ledger WHERE node_id = ?", (node_id,))
    row = cursor.fetchone()
    _release_conn(conn)
    return row[0] if row else None

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
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("UPDATE ledger SET balance = %s WHERE node_id = %s", (balance, node_id))
    else:
        cursor.execute("UPDATE ledger SET balance = ? WHERE node_id = ?", (balance, node_id))
    conn.commit()
    _release_conn(conn)

def add_balance(node_id: str, amount: float):
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute("UPDATE ledger SET balance = balance + %s WHERE node_id = %s", (amount, node_id))
    else:
        cursor.execute("UPDATE ledger SET balance = balance + ? WHERE node_id = ?", (amount, node_id))
    conn.commit()
    _release_conn(conn)

def deduct_balance(node_id: str, amount: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE ledger SET balance = balance - %s WHERE node_id = %s AND balance >= %s",
            (amount, node_id, amount)
        )
    else:
        cursor.execute(
            "UPDATE ledger SET balance = balance - ? WHERE node_id = ? AND balance >= ?",
            (amount, node_id, amount)
        )
    updated = cursor.rowcount
    conn.commit()
    _release_conn(conn)
    return updated > 0

def create_task(task_id: str, consumer_id: str, payload: str, bounty: float, status: str, target_node: Optional[str], model_requirement: Optional[str], created_at: float, result_payload: Optional[str] = None, payload_uri: Optional[str] = None):
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "INSERT INTO tasks (task_id, consumer_id, provider_id, payload, bounty, status, target_node, model_requirement, result_payload, payload_uri, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (task_id, consumer_id, None, payload, bounty, status, target_node, model_requirement, result_payload, payload_uri, created_at, created_at)
        )
    else:
        cursor.execute(
            "INSERT INTO tasks (task_id, consumer_id, provider_id, payload, bounty, status, target_node, model_requirement, result_payload, payload_uri, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, consumer_id, None, payload, bounty, status, target_node, model_requirement, result_payload, payload_uri, created_at, created_at)
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

def requeue_task_if_assigned(task_id: str, updated_at: float) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    if _is_postgres():
        cursor.execute(
            "UPDATE tasks SET status = 'bidding', provider_id = NULL, updated_at = %s WHERE task_id = %s AND status = 'assigned'",
            (updated_at, task_id)
        )
    else:
        cursor.execute(
            "UPDATE tasks SET status = 'bidding', provider_id = NULL, updated_at = ? WHERE task_id = ? AND status = 'assigned'",
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
    if _is_postgres():
        cursor.execute(
            "INSERT INTO escrows (task_id, consumer_id, provider_id, amount, status, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (task_id) DO NOTHING",
            (task_id, consumer_id, None, amount, "held", created_at, created_at)
        )
    else:
        cursor.execute(
            "INSERT OR IGNORE INTO escrows (task_id, consumer_id, provider_id, amount, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, consumer_id, None, amount, "held", created_at, created_at)
        )
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
        cursor.execute("UPDATE ledger SET balance = balance + %s WHERE node_id = %s", (amount, provider_id))
    else:
        cursor.execute(
            "UPDATE escrows SET provider_id = ?, status = ?, updated_at = ? WHERE task_id = ?",
            (provider_id, "released", updated_at, task_id)
        )
        cursor.execute("UPDATE ledger SET balance = balance + ? WHERE node_id = ?", (amount, provider_id))
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
        cursor.execute("UPDATE ledger SET balance = balance + %s WHERE node_id = %s", (amount, consumer_id))
    else:
        cursor.execute(
            "UPDATE escrows SET status = ?, updated_at = ? WHERE task_id = ?",
            ("refunded", updated_at, task_id)
        )
        cursor.execute("UPDATE ledger SET balance = balance + ? WHERE node_id = ?", (amount, consumer_id))
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
    if _is_postgres():
        cursor.execute(
            "UPDATE ledger SET balance = balance - %s WHERE node_id = %s AND balance >= %s",
            (amount, provider_id, amount)
        )
    else:
        cursor.execute(
            "UPDATE ledger SET balance = balance - ? WHERE node_id = ? AND balance >= ?",
            (amount, provider_id, amount)
        )
    if cursor.rowcount == 0:
        _release_conn(conn)
        return {"status": "insufficient"}
    if _is_postgres():
        cursor.execute(
            "UPDATE escrows SET status = %s, updated_at = %s WHERE task_id = %s",
            ("chargeback", updated_at, task_id)
        )
        cursor.execute("UPDATE ledger SET balance = balance + %s WHERE node_id = %s", (amount, consumer_id))
    else:
        cursor.execute(
            "UPDATE escrows SET status = ?, updated_at = ? WHERE task_id = ?",
            ("chargeback", updated_at, task_id)
        )
        cursor.execute("UPDATE ledger SET balance = balance + ? WHERE node_id = ?", (amount, consumer_id))
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
