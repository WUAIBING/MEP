# Contributing to MEP

Thank you for your interest in contributing to MEP (Miao Exchange Protocol)! This document will help you get started.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Testing](#testing)
- [Code Style](#code-style)
- [Submitting Changes](#submitting-changes)
- [Project Structure](#project-structure)

## Code of Conduct

Be respectful, inclusive, and collaborative. We welcome contributions from everyone regardless of background or experience level.

## Getting Started

### Prerequisites

- Python 3.10 or higher
- Git
- Docker (optional, for running Hub with Postgres)

### Setup

```bash
# Clone the repository
git clone https://github.com/WUAIBING/MEP.git
cd MEP

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements-test.txt
```

### Running the Hub Locally

**Environment Variables (set before starting hub):**
```bash
# Admin key for admin endpoints (registration approval, etc.)
export MEP_ADMIN_KEY=your-admin-key

# Database URL (use explicit SQLite path)
export MEP_DATABASE_URL="sqlite:///mep.db"
```

**Note**: Environment variable changes require a hub restart to take effect.

**Option 1: Docker + Postgres (Recommended for production-like testing)**
```bash
docker-compose up -d --build
```

**Option 2: Local dev with SQLite (Faster for development)**
```bash
cd hub
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:
```bash
curl http://localhost:8000/health
```

### Node Registration Flow

**Important**: As of PR #217, new node registrations require admin approval before receiving balance.

**Registration Process:**
1. Register node → Status: `pending`, Balance: `0.0`
2. Admin approves node → Status: `approved`, Balance: `0.0`
3. Node can now participate in tasks

**For local development/testing:**
- The test helper `_register()` in `tests/test_hub_api.py` auto-approves nodes by default
- For production hubs, use the admin endpoints:
  - `POST /admin/approve-registration` - Approve a pending registration
  - `GET /admin/pending-registrations` - List pending registrations
- Use the `MEP_ADMIN_KEY` set above for admin authentication

**Admin endpoints require the admin key in headers:**
```bash
curl -X POST http://localhost:8000/admin/approve-registration \
  -H "x-mep-admin-key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"node_id": "node_xxx"}'
```

**Note**: The `README.md` quickstart section should also be updated to reflect the pending-approval registration flow for consistency with the current implementation.

## Development Workflow

### Branch Strategy

- `main` - Production branch, always stable
- `prod/*` - Production feature branches (e.g., `prod/fix-admin-timing-attack-pr218`)
- `pr-*` - Experimental branches

### Creating a Feature Branch

```bash
git checkout main
git pull
git checkout -b prod/your-feature-name
```

### Making Changes

1. Make your changes
2. Run tests locally
3. Run linting
4. Commit with clear messages
5. Push and create PR

### Commit Message Format

Use conventional commits:

```
feat(hub): add new endpoint for task verification
fix(node): resolve WebSocket reconnection issue
docs(readme): update quickstart guide
test(hub): add regression test for admin approval
```

## Testing

### Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_hub_api.py -v

# With coverage
python -m pytest tests/ -v --cov --cov-report=term-missing
```

### Linting

```bash
# Check code style
ruff check hub/ node/ core/ tests/

# Auto-fix issues
ruff check --fix hub/ node/ core/ tests/
```

### Test Structure

| File | What it tests |
|------|--------------|
| `tests/test_hub_auth.py` | Ed25519 signature verification, node ID derivation |
| `tests/test_hub_api.py` | Hub API endpoints (register, balance, task lifecycle) |
| `tests/test_max_purchase_price.py` | Data market budget safety logic |
| `tests/test_sentinel_engineer_v2.py` | Autonomous agent: parser, circuit breaker, code executor |

### Integration Tests

Integration tests require a running Hub:

```bash
# Terminal 1: Start Hub
docker-compose up

# Terminal 2: Run integration tests
python node/test_auction.py
python node/test_three_markets.py
python node/test_dm.py
```

## Code Style

### Python

- Use Python 3.10+ type hints where appropriate
- Follow PEP 8 style guide
- Maximum line length: 88 characters (ruff default)
- Use `ruff` for linting and formatting

### Line Endings

- Use CRLF (`\r\n`) line endings on Windows
- Use LF (`\n`) line endings on Unix/Linux
- The project uses `.editorconfig` to enforce this

### Key Patterns

- **Database operations**: Use keyword arguments to avoid positional-argument corruption
- **Financial calculations**: The spec mandates integer nanoseconds, but current implementation uses floats (this is a known issue being addressed)
- **Security**: Always use `hmac.compare_digest()` for secret comparisons

## Submitting Changes

### Pull Request Process

1. Create a feature branch from `main`
2. Make your changes
3. Ensure all tests pass locally
4. Push to your fork or the main repo
5. Create a pull request with:
   - Clear title (e.g., `feat(hub): add new endpoint`)
   - Description of changes
   - Test plan checklist
   - References to related issues

### PR Checklist

- [ ] Tests pass locally (`python -m pytest tests/ -v`)
- [ ] Linting passes (`ruff check hub/ node/ core/ tests/`)
- [ ] Commit messages follow conventional format
- [ ] Documentation updated if needed
- [ ] No sensitive data (API keys, secrets) committed

### CI/CD

Pull requests automatically run:
- Lint check (ruff)
- Unit tests (pytest) on Ubuntu and Windows
- PRs must pass CI before merging

## Project Structure

```
MEP/
├── bot/              # Autonomous agent components
├── clients/          # Client adapters for various platforms
├── core/             # Core protocol and utilities
├── docs/             # Design documents and specs
├── hub/              # Hub server implementation
│   ├── main.py       # FastAPI application (2,934 lines - needs refactoring)
│   ├── db.py         # Database operations
│   └── auth.py       # Authentication and signature verification
├── hub_data/         # Hub data directory (SQLite, logs)
├── node/             # Node runtime implementation
├── scripts/          # Utility scripts
├── skills/           # Node skills and capabilities
├── tests/            # Test suite
└── venv/             # Virtual environment (not committed)
```

## Good First Issues

Looking for something to work on? Check these issues:

- [ ] [P1] Refactor hub/main.py — split 127KB monolith into sub-packages (#179)
- [ ] [P1] Add CONTRIBUTING.md and Makefile for contributor onboarding (#178)
- [ ] [P0] Fix float precision in balance/escrow arithmetic (from #187 audit)
- [ ] [P0] Fix synchronous DB in async context (from #187 audit)

## Getting Help

- Check existing documentation: `README.md`, `TESTING.md`, `DEPLOYMENT.md`
- Review the codebase: `hub/main.py`, `hub/db.py`, `node/`
- Open an issue for bugs or feature requests
- Join discussions in GitHub Issues

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
