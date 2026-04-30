# tests/conftest.py
import pytest

pytest_plugins = ("pytest_asyncio",)

# Run all async tests with auto mode (detect automatically)
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
