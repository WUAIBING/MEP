# Contributing to MEP

Welcome! We're excited to have you contribute to the MEP (Multi-Entity Protocol) project. This guide will help you set up your development environment and follow our contribution practices.

## Prerequisites

Before you start, ensure you have the following installed:

- **Python 3.10+** — [Download here](https://www.python.org/downloads/)
- **Docker & Docker Compose** — [Install Docker Desktop](https://www.docker.com/products/docker-desktop)
- **PostgreSQL 13+** — [Download here](https://www.postgresql.org/download/) (or use Docker)
- **Git** — [Download here](https://git-scm.com/)

## Development Environment Setup

### One-Command Setup

```bash
# Clone the repository
git clone <repository-url>
cd MEP

# Create virtual environment and install dependencies
python -m venv venv

# Activate virtual environment
# On Linux/macOS:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install development dependencies
pip install -e ".[dev]"
```

### Verify Installation

```bash
# Check Python version
python --version

# Run a quick test
pytest tests/ -v --collect-only
```

## Branch Naming Conventions

Use the following prefixes for your branches:

| Prefix | Purpose | Example |
|--------|---------|---------|
| `feat/` | New feature | `feat/add-node-discovery` |
| `fix/` | Bug fix | `fix/authenticate-claude-adapter` |
| `docs/` | Documentation updates | `docs/update-deployment-guide` |
| `chore/` | Build, dependencies, config | `chore/upgrade-dependencies` |
| `refactor/` | Code refactoring | `refactor/simplify-task-envelope` |

Example:
```bash
git checkout -b feat/add-mesh-monitoring
```

## Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types
- **feat**: A new feature
- **fix**: A bug fix
- **docs**: Documentation changes
- **chore**: Build, dependencies, tooling
- **refactor**: Code refactoring without feature changes
- **test**: Test-related changes
- **perf**: Performance improvements

### Examples

```
feat(node): add sentiment analysis to broadcast_opinion

- Implemented ML model for opinion scoring
- Added caching for performance

Closes #123
```

```
fix(hub): resolve authentication token expiration

Fixed a race condition in token refresh logic that could cause
service disruption during high load periods.
```

```
docs(README): clarify PostgreSQL setup steps
```

## Running Tests

### Run All Tests
```bash
pytest tests/ -v
```

### Run Tests for a Specific Module
```bash
pytest tests/test_node_client.py -v
```

### Run with Coverage
```bash
pytest tests/ --cov=node --cov=hub --cov=core
```

### Run Specific Test
```bash
pytest tests/test_node_client.py::test_authenticate -v
```

## Code Quality

### Linting
```bash
ruff check .
```

### Auto-Fix Issues
```bash
ruff check . --fix
```

### Type Checking
If using type hints, run:
```bash
mypy node/ hub/ core/ clients/
```

## Building & Running Services

### Start Hub with Docker
```bash
docker-compose up -d --build
```

### Check Service Status
```bash
docker-compose ps
```

### View Logs
```bash
docker-compose logs -f hub
```

### Stop Services
```bash
docker-compose down
```

## Using Make Commands

We provide a `Makefile` with common development tasks:

```bash
# Run all tests
make test

# Run linting checks
make lint

# Start hub service with Docker
make hub

# Clean up Python cache files
make clean
```

## Pull Request Checklist

Before submitting a pull request, ensure:

- [ ] **Branch naming** follows conventions (`feat/`, `fix/`, `docs/`, `chore/`)
- [ ] **Commits** follow Conventional Commits format
- [ ] **Tests pass**: `pytest tests/ -v`
- [ ] **Linting passes**: `ruff check .`
- [ ] **Documentation** is updated for new features
- [ ] **No unnecessary files** in diff:
  - [ ] `venv/` directory excluded
  - [ ] `*.log` files excluded
  - [ ] `__pycache__/` directories excluded
  - [ ] `.env` files with secrets excluded
- [ ] **Changelog** entry added if applicable

### Creating a PR

1. Push your branch: `git push origin feat/your-feature`
2. Open a PR on GitHub with a clear description
3. Link related issues: "Closes #123"
4. Wait for CI/CD checks to pass
5. Address review feedback promptly

## Git Workflow Example

```bash
# Create a feature branch
git checkout -b feat/add-reputation-decay

# Make changes and commit
git add .
git commit -m "feat(reputation): implement exponential decay model

- Decays node reputation over 30-day period
- Prevents stale reputation from biasing auctions
- Includes test coverage

Closes #456"

# Push and create PR
git push origin feat/add-reputation-decay
```

## Common Issues

### Virtual Environment Not Activating
- **Linux/macOS**: Ensure you run `source venv/bin/activate` (not `venv/bin/activate`)
- **Windows**: Use `venv\Scripts\activate` (not `/`)

### Import Errors
- Delete `__pycache__/` folders: `make clean`
- Reinstall dependencies: `pip install -e ".[dev]"`

### Docker Compose Port Conflicts
- Check what's using the port: `lsof -i :5432` (Linux/macOS)
- Stop conflicting services or change `docker-compose.yml` ports

## Getting Help

- Check existing [GitHub issues](../issues)
- Read the [README.md](../README.md) and design docs in `/docs`
- Ask in discussions or create a new issue for guidance

## Code of Conduct

Please be respectful and inclusive. We follow a standard code of conduct for open-source projects.

---

**Happy contributing! 🚀**
