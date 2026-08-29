# MEP (Miao Exchange Protocol)  Professional Code Review 

**Repository:** [github.com/WUAIBING/MEP](https://github.com/WUAIBING/MEP)
**Review Date:** May 5, 2026
**Reviewer:** Monica AI
**Scope:** `hub/main.py`, `hub/db.py`, `hub/auth.py`, `hub/models.py`, `hub/logger.py`, `hub/requirements.txt`, `clients/shared/mep_client.py`, `clients/shared/stdio_adapter.py`, `node/mep_provider.py`, `skills/quickstart_provider.py`, `docker-compose.yml`, `README.md`
**Baseline:** Comparison against the May 2026 review (v0.1.2, score 8.1 / 10)

---

## Executive Summary

MEP continues to demonstrate the same architectural ambition observed in the previous review: a FastAPI-based clearinghouse, Ed25519 cryptographic identity, dual-database support (SQLite / PostgreSQL), an escrow ledger, and agent-to-agent task auction semantics. The repository version remains pinned at **0.1.2** in `app = FastAPI(... version="0.1.2")`, which suggests this review captures the same release line as the prior audit. Several of the highest-priority findings from May 2026  most notably the **synchronous database driver inside an async event loop**, the **absence of a test suite**, the **monolithic `main.py`**, and **unpinned dependencies**  remain open.

The codebase still earns credit for thoughtful crypto design, clean normalisation patterns, and a strong README. However, the gap between "interesting research project" and "production-ready protocol implementation" is defined almost entirely by the unresolved items below. The overall score for this review is **8.0 / 10**, marginally below the previous 8.1 because two prior gaps (resource limits in Docker Compose, hub-side healthcheck) have not closed while new client-side concerns have surfaced.

---

## 1. Architecture Overview

### 1.1 Component Map

```

  Clients (stdio / Discord / Telegram / Feishu / WeChat )    
  clients/shared/mep_client.py    MEPIdentity (Ed25519)      
  clients/shared/stdio_adapter.py                              

                          HTTPS / WSS

                     MEP Hub  (FastAPI 0.1.2)                  
  main.py  auth.py  db.py  logger.py  models.py            
                                                               
  In-memory:  active_tasks  completed_tasks  connected_nodes 
              rate_limits  mesh_assemblies                    
  Locks:      task_lock  node_lock  mesh_lock                
                                                               
  Persistent: PostgreSQL (prod) / SQLite (dev)                

                         
                  
                   Provider      node/mep_provider.py
                   Nodes (WS)    skills/quickstart_provider.py
                  
```

### 1.2 Status of Prior Findings

| ID | Prior Finding | Current Status |
|----|---------------|----------------|
| DB-1 | Migrate to async DB driver | **Open**  `db.py` still imports `sqlite3` and `psycopg2` synchronously |
| DB-2 | Externalise in-memory state to Redis | **Open**  `active_tasks`, `connected_nodes`, `rate_limits`, `mesh_assemblies` still in-process |
| QUAL-1 | Refactor `main.py` into route modules | **Open**  single-file hub structure unchanged |
| QUAL-6 | Pin dependency versions | **Open**  `requirements.txt` lists six packages with zero version constraints |
| QUAL-7 | Migrate from `on_event` to `lifespan` | **Likely open**  imports do not show `asynccontextmanager` from `contextlib` |
| TEST-1 | Implement test suite | **Open**  no `tests/` directory or `conftest.py` referenced anywhere in the visible tree |
| OPS-3 | Docker Compose resource limits | **Open**  no `mem_limit`, `cpus`, or `ulimits` on `mep-hub` or `postgres` |
| OPS-4 | Hub healthcheck in Docker Compose | **Open**  `mep-hub` service has no `healthcheck` block |
| MESH-1 | `MESH_ALLOWED_TRIGGERS` configurable | Indeterminate from visible excerpt |
| QUAL-2 | Pydantic field validators (`bounty`, `rating`) | **Partially open**  `expires_in_seconds` now uses `Field(ge=1)`, but `bounty: float` still has no bounds and `rating: int` still has no `ge=1, le=5` |

---

## 2. Security Analysis

### 2.1 Strengths Confirmed

**Ed25519 signature verification (`auth.py`).** The `verify_signature` function correctly enforces a 300-second timestamp skew, validates that the loaded key is an instance of `Ed25519PublicKey`, and catches a precise tuple of exceptions (`InvalidSignature`, `ValueError`, `TypeError`, `binascii.Error`). The deterministic `derive_node_id` derives a 12-character SHA-256 prefix from the PEM string, giving stable, unforgeable identities.

**Cryptography library choice.** The use of `cryptography.hazmat.primitives.asymmetric.ed25519` is the right primitive for an agent-to-agent protocol  fast, deterministic, small signatures, no parameter pitfalls.

**Body size cap.** `MAX_BODY_BYTES = 200_000` and `MAX_PAYLOAD_CHARS = 20_000` provide layered defence against memory exhaustion via large request bodies.

**Database connection pooling.** `db.py` uses `psycopg2.pool.SimpleConnectionPool` with configurable `MEP_PG_POOL_MIN` / `MEP_PG_POOL_MAX`. This is correct for bounded concurrency, even though it does not address the async-blocking concern (see 3.1).

### 2.2 Issues and Recommendations

**[SEC-1] Replay risk on the 300-second skew window.** `auth.verify_signature` accepts any signed payload within 300 seconds. Because the signed message is `f"{payload_str}{timestamp}"` with no nonce or request-id, an attacker who captures one valid HTTP request can replay it within five minutes. Recommendation: add a server-side nonce cache (Redis or in-memory LRU keyed by `(node_id, signature)`) and reject duplicates within the skew window.

```python
# pseudocode
seen: dict[tuple[str, str], float] = {}
key = (node_id, signature_b64)
if key in seen:
    return False
seen[key] = time.time()
```

**[SEC-2] `db.py` falls back silently when `psycopg2` is missing.** The `try/except ImportError: psycopg2 = None` pattern means a misconfigured production deployment that *thinks* it is running PostgreSQL will silently revert to SQLite or raise only on first use. A deployment guard at startup (`if DB_URL and psycopg2 is None: raise SystemExit(...)`) would fail loud and early.

**[SEC-3] `sqlite3.connect(..., check_same_thread=False)` is permissive.** Disabling SQLite's thread-safety check is necessary in async contexts but only safe when callers serialise access. Combined with the absence of a single SQLite-level lock in `db.py`, concurrent writes from different coroutines can cause `database is locked` errors under contention. Recommendation: wrap SQLite writes in an `asyncio.Lock` or migrate to `aiosqlite` which handles this cleanly.

**[SEC-4] `node/mep_provider.py` defaults to plaintext HTTP.** The provider node defaults are `HUB_URL=http://localhost:8000` and `WS_URL=ws://localhost:8000`. This is acceptable for local dev, but the client `mep_client.py` correctly defaults to `https://` and `wss://`. The asymmetry suggests the provider was written for development first and never tightened. Recommendation: in production builds, refuse to start unless `HUB_URL` begins with `https://` (or set an explicit `MEP_ALLOW_INSECURE=1` flag).

**[SEC-5] `requests.Session()` in `mep_client.py` has no certificate pinning.** Combined with the federation concern from the prior review, MEP relies entirely on the system trust store. For a federated network where peer hubs are known in advance, configurable certificate fingerprint pinning would harden the threat model.

**[SEC-6] Audit log path is fixed.** `logger.py` hardcodes `LOG_DIR = "logs"` and creates the directory at import time. If the working directory changes between import and runtime (which can happen in containerised setups), logs may end up in unexpected locations. Recommendation: use `os.path.dirname(os.path.abspath(__file__))` as the base, or make `LOG_DIR` an environment variable.

---

## 3. Concurrency and Performance

### 3.1 The Synchronous Database Driver  Still the Single Most Important Issue

The previous review flagged this clearly, and `db.py` confirms it remains unaddressed:

```python
import sqlite3
import psycopg2
from psycopg2 import pool
```

Every database call from `main.py` runs synchronously inside the FastAPI event loop. A 200 ms slow query stalls *every* in-flight WebSocket connection and HTTP request on the same worker. Under realistic concurrency (50+ providers connected, several auctions per second), this manifests as random WebSocket pings missing their deadline and clients reconnecting unnecessarily.

**Migration path (lower risk first):**

| Step | Action | Effort |
|------|--------|--------|
| 1 | Wrap every `db.py` call in `asyncio.to_thread()` from `main.py` | 1 day |
| 2 | Replace `psycopg2` with `asyncpg`, `sqlite3` with `aiosqlite` | 2C3 days |
| 3 | Convert `db.py` functions to `async def` | 1 day |

Step 1 alone removes the blocking hazard at the cost of a thread-pool dependency, and is the recommended first move before any traffic growth.

### 3.2 In-Memory State Still Blocks Horizontal Scaling

`main.py` declares five process-local dictionaries (`active_tasks`, `completed_tasks`, `connected_nodes`, `rate_limits`, `mesh_assemblies`). The locks (`task_lock`, `node_lock`, `mesh_lock`) coordinate within a process but cannot span instances. Two `mep-hub` containers behind a load balancer would each see a partial view of the world.

This is acceptable for the current research-grade deployment but should be planned before the first multi-instance rollout.

### 3.3 `node/mep_provider.py` Mixes Sync `requests` With `async` Code

The provider uses `requests.Session()` for registration *and* `websockets` for the live channel. Inside an async event loop, every `self.session.post(...)` call blocks the loop. Recommendation: use `httpx.AsyncClient` (already idiomatic in `mep_client.py`'s style) for symmetry, or wrap the existing `requests` calls in `asyncio.to_thread()`.

### 3.4 `mep_client.py` Uses `requests` Inside `asyncio.to_thread`

This is the *correct* pattern for keeping a sync HTTP library off the event loop, and it is applied consistently. The remaining concern is that `httpx.AsyncClient` would eliminate the thread-pool round-trip and reduce p99 latency for high-frequency calls. Not urgent, but a clean improvement.

---

## 4. Code Quality

### 4.1 Positive Observations

**Clean, narrow `auth.py`.** Twenty-odd lines, one job, no surprises. Excellent module boundary.

**`logger.py` is well-structured.** `JSONFormatter`, rotating file handler (10 MB  30 backups), dual file + console output, UTC timestamps. The `setup_logger` idempotency check (`if logger.handlers: return logger`) prevents double-handler bugs in test reloads.

**`models.py` adds one field validator.** `expires_in_seconds: Optional[int] = Field(default=None, ge=1)` shows the team has started adopting the prior recommendation. This is the right direction.

**Quickstart provider (`skills/quickstart_provider.py`).** Argparse with sensible env-var-backed defaults, clear separation between compute, chat, and data flows. This is a developer-experience win.

### 4.2 Issues and Recommendations

**[QUAL-1] Persistent monolithic `main.py`.** Still a single file. The recommended split from the May 2026 review remains valid:

| Proposed Module | Content |
|----------------|---------|
| `hub/routes/tasks.py` | task lifecycle endpoints |
| `hub/routes/registry.py` | registry, reputation, balance |
| `hub/routes/federation.py` | peer hubs |
| `hub/routes/mesh.py` | mesh assembly |
| `hub/routes/admin.py` | disputes, events, logs |
| `hub/services/assignment.py` | scoring, RFC selection |
| `hub/middleware.py` | security headers, trusted host, IP allow-list |

**[QUAL-2] `requirements.txt` has zero version pins.**

```
fastapi
uvicorn
pydantic
websockets
cryptography
psycopg2-binary
```

A `pip install -r requirements.txt` today and six months from now will yield different code. For a security-sensitive component (`cryptography`, `psycopg2-binary`), this is risky. Recommendation:

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic==2.9.2
websockets==13.1
cryptography==43.0.3
psycopg2-binary==2.9.10
```

Use `pip-compile` (from `pip-tools`) or `uv lock` to maintain a transitive lockfile.

**[QUAL-3] `models.py` still under-validates.** `bounty: float` has no `ge` / `le`, `rating: int` (in `ReputationSubmit`) has no `ge=1, le=5`. These are Pydantic-level fixes worth ~10 minutes each.

**[QUAL-4] `requirements.txt` is missing `httpx` and `requests`.** `mep_client.py` imports `requests`, `node/mep_provider.py` imports `requests` and `urllib3`. Neither is declared in the hub's requirements. This is forgivable because the hub does not need them, but a top-level `requirements.txt` for the client side should exist (and currently does not appear in the inspected tree).

**[QUAL-5] `node/mep_provider.py` uses `sys.path.append`.** Manipulating `sys.path` at import time works but is a packaging smell. Adding a minimal `pyproject.toml` and installing the package with `pip install -e .` is the standard fix and removes the need for path hacks across `clients/`, `node/`, and `skills/`.

**[QUAL-6] `docker-compose.yml` uses escaped hyphens (`\-`).** The fetched content shows `\- "8000:8000"` and similar entries. Either the file genuinely contains backslash-escaped YAML lines (which is invalid YAML and would fail `docker compose up`), or the raw view introduced escapes during transit. Worth verifying locally  if the file *does* contain `\-`, `docker compose config` will reject it.

**[QUAL-7] FastAPI `lifespan` migration.** The `from fastapi import FastAPI, ...` line shows no import of `asynccontextmanager`, suggesting the codebase still uses the deprecated `@app.on_event("startup" | "shutdown")` decorators. Migration is straightforward and removes a deprecation warning that will become an error in future FastAPI releases.

---

## 5. Testing

No `tests/` directory is referenced anywhere in the inspected tree. **This remains the single largest gap relative to a production-ready protocol implementation.**

A minimum viable bootstrap (recommended order):

| Test | Priority | Notes |
|------|----------|-------|
| `test_auth.py::test_verify_signature_valid` | Critical | golden-path Ed25519 sign/verify |
| `test_auth.py::test_verify_signature_skew_rejected` | Critical | timestamp > 300 s in past/future |
| `test_auth.py::test_verify_signature_wrong_key_type` | High | reject RSA, ECDSA |
| `test_db.py::test_escrow_atomicity` | Critical | concurrent submit + cancel must not double-spend |
| `test_tasks.py::test_submit_bid_complete_lifecycle` | Critical | `httpx.AsyncClient` against `TestClient` |
| `test_tasks.py::test_idempotency_replay` | High | duplicate `x_mep_idempotency_key` returns cached response |
| `test_disputes.py::test_chargeback_balance_check` | High | dispute resolution path |
| `test_websocket.py::test_handshake_replay` | Medium | covers SEC-1 once fixed |

A `conftest.py` with an in-memory SQLite fixture and an authenticated `MEPIdentity` fixture is sufficient to start. Three to five engineering days of test infrastructure work would meaningfully change the project's risk profile.

---

## 6. Observability and Operations

### 6.1 What Works

The dual logging setup (`hub.json` for structured events, `ledger_audit.log` for ledger operations) gives operators two complementary views. Rotation (10 MB  30 backups = ~300 MB ceiling per log) prevents disk exhaustion. The PostgreSQL container has a proper `pg_isready` healthcheck.

### 6.2 Gaps

**[OPS-1] No Prometheus `/metrics` endpoint.** The hub exposes `/health` (per prior review) but no scrape-friendly format. Adding `prometheus-fastapi-instrumentator` is a single-line change that yields request duration histograms, error counters, and custom gauges.

**[OPS-2] `mep-hub` service has no `healthcheck`.** Without it, `depends_on` is shallow  the container is "ready" the instant the process starts. Adding:

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -fsS http://localhost:8000/health || exit 1"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 15s
```

enables `depends_on: condition: service_healthy` from any future client containers.

**[OPS-3] No resource limits.** Neither container declares `mem_limit`, `cpus`, or `pids_limit`. A runaway query or a Sybil-style connection flood could exhaust host memory. Recommendation:

```yaml
mep-hub:
  deploy:
    resources:
      limits:
        memory: 512M
        cpus: "1.0"
```

**[OPS-4] `volumes: ./hub_data:/app/logs` mixes logs and data.** The host directory `./hub_data` holds both `pgdata/` and the hub's `logs/`. Separating these into `./hub_logs` and `./hub_data/pgdata` would simplify backup policies (logs are ephemeral, Postgres data is not).

**[OPS-5] No `restart: unless-stopped` semantics review.** Both services use `restart: always`, which is fine for production but causes annoying restart loops in development if Postgres credentials are wrong. Consider environment-specific compose overrides.

---

## 7. Client and Provider Code

### 7.1 `clients/shared/mep_client.py`

The default `HUB_URL = "https://mep-hub.silentcopilot.ai"` is reasonable. Heartbeat interval is configurable. `asyncio.to_thread` wraps `requests` cleanly. **One concern:** `self.session = requests.Session()` is created in `__init__` but there is no visible `aclose()` in the inspected excerpt  connection-pool leaks are possible across long-lived bot sessions.

### 7.2 `clients/shared/stdio_adapter.py`

Clean, small, single responsibility. The `MEP_BOT_KEY_PATH` defaulting to `tempfile.gettempdir()` is a developer-experience choice that **should not be the production default**  keys in `/tmp` are wiped on reboot, which silently re-registers a bot under a new node ID and abandons its reputation. Recommendation: default to `~/.mep/<platform>.pem` and warn loudly if the path is in `/tmp`.

### 7.3 `node/mep_provider.py`

Good: explicit `urllib3.util.Retry` policy (`total=5, backoff_factor=1, status_forcelist=[502, 503, 504]`) for transient failures. Bad: defaults to plaintext HTTP/WS (see SEC-4). Also uses `sys.path.append(...)` for imports  consider proper packaging.

### 7.4 `skills/quickstart_provider.py`

Best-presented file in the client tree. Argparse + env-var defaults + three market modes (compute / chat / data) is exactly the right onboarding surface. No issues found.

---

## 8. Priority Fix Checklist

| ID | Severity | Category | Description | Effort |
|----|----------|----------|-------------|--------|
| DB-1 | Critical | Performance | Migrate to async DB driver or wrap calls in `asyncio.to_thread` | 1C3 days |
| TEST-1 | Critical | Quality Assurance | Bootstrap pytest test suite with auth + lifecycle coverage | 3C5 days |
| SEC-1 | High | Security | Add nonce cache to defeat 300-second replay window | 2 h |
| SEC-2 | High | Security | Fail loud when `psycopg2` missing but `MEP_DATABASE_URL` set | 15 min |
| SEC-4 | High | Security | Refuse plaintext HTTP in provider unless explicit override | 30 min |
| QUAL-1 | High | Maintainability | Refactor `main.py` into route + service modules | 1C2 days |
| QUAL-2 | High | Maintainability | Pin all dependency versions in `requirements.txt` | 1 h |
| QUAL-3 | Medium | Code Quality | Add Pydantic `Field(ge=..., le=...)` for `bounty` and `rating` | 30 min |
| QUAL-5 | Medium | Packaging | Add `pyproject.toml`; remove `sys.path.append` hacks | 2 h |
| QUAL-6 | Medium | Operations | Verify `docker-compose.yml` is not literally backslash-escaped | 5 min |
| QUAL-7 | Medium | Code Quality | Migrate `on_event`  `lifespan` context manager | 30 min |
| OPS-1 | Medium | Observability | Add Prometheus `/metrics` via `prometheus-fastapi-instrumentator` | 2 h |
| OPS-2 | Medium | Operations | Add `healthcheck` to `mep-hub` Compose service | 15 min |
| OPS-3 | Medium | Operations | Add memory + CPU limits to both Compose services | 30 min |
| OPS-4 | Low | Operations | Split log volume from Postgres data volume | 15 min |
| SEC-3 | Medium | Security | Serialise SQLite writes via `asyncio.Lock` or move to `aiosqlite` | 2 h |
| SEC-5 | Low | Security | Optional certificate pinning for federated peers | 4 h |
| SEC-6 | Low | Operations | Anchor `LOG_DIR` to absolute path or env var | 15 min |
| Client-1 | Low | Reliability | Default key path away from `/tmp` to preserve identity across reboots | 30 min |
| Client-2 | Low | Performance | Replace `requests` with `httpx.AsyncClient` in client + provider | 4 h |

---

## 9. Overall Assessment

MEP remains a coherent, ambitious research codebase with a strong cryptographic spine and a clear product vocabulary (compute / chat / data markets, SECONDS as the unit of value). Since the prior review the team has begun adopting Pydantic field validators (`expires_in_seconds`), which is encouraging  but the **headline structural items have not moved**: synchronous DB driver, no test suite, monolithic `main.py`, unpinned dependencies, no Compose resource limits, no hub-side healthcheck.

In honesty, the order of operations now matters more than any single fix. The recommended sequence is:

1. **Pin dependencies** (1 hour, prevents tomorrow's surprise breakage).
2. **Wrap every DB call in `asyncio.to_thread`** (1 day, removes the worst event-loop hazard at low risk).
3. **Bootstrap a five-test pytest suite covering auth + task lifecycle** (2 days, makes every subsequent refactor safer).
4. **Then** tackle `main.py` decomposition, async DB migration, and Redis externalisation in that order.

Doing items 1C3 in a single sprint would shift the project's overall risk profile materially and make the score in the next review climb meaningfully.

**Score: 8.0 / 10**

| Dimension | Score | Notes |
|-----------|-------|-------|
| Architecture | 8.5 / 10 | Clean separation of concerns; market design is coherent |
| Security | 8.0 / 10 | Strong crypto; replay window and silent fallbacks remain |
| Performance | 6.5 / 10 | Synchronous DB still blocks the event loop |
| Code Quality | 7.5 / 10 | Consistent patterns; monolithic hub, sparse model validators |
| Observability | 7.5 / 10 | Good logging; no Prometheus metrics |
| Testing | 2.0 / 10 | No test suite found |
| Documentation | 9.0 / 10 | README is excellent and the three-market diagram is memorable |

---

## References

[1]: https://github.com/WUAIBING/MEP "MEP GitHub Repository"
[2]: https://raw.githubusercontent.com/WUAIBING/MEP/main/hub/main.py "hub/main.py"
[3]: https://raw.githubusercontent.com/WUAIBING/MEP/main/hub/db.py "hub/db.py"
[4]: https://raw.githubusercontent.com/WUAIBING/MEP/main/hub/auth.py "hub/auth.py"
[5]: https://raw.githubusercontent.com/WUAIBING/MEP/main/hub/models.py "hub/models.py"
[6]: https://raw.githubusercontent.com/WUAIBING/MEP/main/hub/logger.py "hub/logger.py"
[7]: https://raw.githubusercontent.com/WUAIBING/MEP/main/hub/requirements.txt "hub/requirements.txt"
[8]: https://raw.githubusercontent.com/WUAIBING/MEP/main/docker-compose.yml "docker-compose.yml"
[9]: https://raw.githubusercontent.com/WUAIBING/MEP/main/clients/shared/mep_client.py "clients/shared/mep_client.py"
[10]: https://raw.githubusercontent.com/WUAIBING/MEP/main/clients/shared/stdio_adapter.py "clients/shared/stdio_adapter.py"
[11]: https://raw.githubusercontent.com/WUAIBING/MEP/main/node/mep_provider.py "node/mep_provider.py"
[12]: https://raw.githubusercontent.com/WUAIBING/MEP/main/skills/quickstart_provider.py "skills/quickstart_provider.py"

---

> 7215 **Reviewer's note on coverage.** The fetched file contents were partially truncated by the retrieval tool, so findings tied to specific functions in the lower halves of `main.py` and `db.py` (e.g., the federation peer fetcher, mesh role scoring, maintenance worker) could not be re-verified line-for-line in this pass. Treat them as **carried forward from the May 2026 review** unless a follow-up review with full file contents confirms or refutes them. The structural and import-level findings above are based on directly observed code.