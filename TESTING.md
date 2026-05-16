# Testing MEP

## Quick Start

```bash
# Install test dependencies
pip install -r requirements-test.txt

# Run all tests + lint in one command
bash scripts/test.sh
```

## Running Tests

```bash
# All unit tests
python -m pytest tests/ -v

# Single test file
python -m pytest tests/test_hub_api.py -v

# With coverage report
python -m pytest tests/ -v --cov --cov-report=term-missing
```

## Linting

```bash
ruff check hub/ node/ core/ tests/
```

## Test Structure

| File | What it tests |
|------|--------------|
| `tests/test_hub_auth.py` | Ed25519 signature verification, node ID derivation |
| `tests/test_hub_api.py` | Hub API endpoints (register, balance, task lifecycle) |
| `tests/test_max_purchase_price.py` | Data market budget safety logic |
| `tests/test_sentinel_engineer_v2.py` | Autonomous agent: parser, circuit breaker, code executor |

## Integration Tests

Integration tests in `node/test_*.py` require a running Hub:

```bash
# Terminal 1: Start Hub
docker-compose up

# Terminal 2: Run integration tests
python node/test_auction.py
python node/test_three_markets.py
python node/test_dm.py
```

### 3-Market Smoke Test

Use `node/test_three_markets.py` before real-world node-to-node testing. It submits current spec-shaped envelopes and exercises:

- compute: sender pays receiver
- chat/DM: targeted zero-bounty delivery
- data: receiver pays sender for `secret_data`

```bash
export HUB_URL=http://localhost:8000
export WS_URL=ws://localhost:8000
python node/test_three_markets.py
```

Expected final balances start from the default 10 SECONDS registration bonus:

- Alice: pays 5 SECONDS for compute, earns 2 SECONDS for data, ends near 7 SECONDS.
- Bob: earns 5 SECONDS for compute, pays 2 SECONDS for data, ends near 13 SECONDS.

### Data-Market Safety

The standard runtime does not auto-buy negative-bounty data by default. To allow a runtime node to buy data during controlled tests, set:

```bash
export MEP_MAX_PURCHASE_PRICE=2.0
```

This means the node may auto-bid on data-market RFCs costing up to 2.0 SECONDS. Keep it unset or `0.0` for normal safety.

### SECONDS vs MEP_NS

Humans and ledger output use `SECONDS`. Spec-shaped task envelopes use integer nanoseconds:

```text
1 SECONDS = 1,000,000,000 MEP_NS
```

For example, `bounty_ns=500000000` means `0.5 SECONDS`. Test output should prefer `SECONDS`; raw `bounty_ns` should be clearly labeled when shown.

## Writing New Tests

1. Create test files in `tests/` with the prefix `test_`
2. Use `unittest.TestCase` or plain pytest functions
3. For hub endpoint tests, use `FastAPI TestClient` (see `test_hub_api.py` for examples)
4. Tests run on both Ubuntu and Windows in CI — avoid hardcoded Unix paths

## CI

Pull requests automatically run lint + tests via GitHub Actions on both Ubuntu and Windows. PRs must pass before merging.
