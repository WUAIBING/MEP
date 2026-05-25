.PHONY: help test lint hub clean install dev-install type-check format

help:
	@echo "MEP Development Targets"
	@echo "======================="
	@echo "  make install      - Install production dependencies"
	@echo "  make dev-install  - Install development dependencies (includes test & lint tools)"
	@echo "  make test         - Run all tests with pytest"
	@echo "  make lint         - Run linting checks with ruff"
	@echo "  make format       - Auto-format code with ruff"
	@echo "  make type-check   - Run type checking with mypy"
	@echo "  make hub          - Start hub service with Docker Compose"
	@echo "  make clean        - Remove Python cache files and directories"
	@echo "  make help         - Show this help message"

install:
	pip install -e .

dev-install:
	pip install -e ".[dev]"

test:
	pytest tests/ -v

lint:
	ruff check .

format:
	ruff check . --fix
	ruff format .

type-check:
	mypy node/ hub/ core/ clients/ --ignore-missing-imports

hub:
	docker-compose up -d --build

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleanup complete"
