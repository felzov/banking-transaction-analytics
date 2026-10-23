"""Fixtures for API tests that need no database."""

from collections.abc import Iterator

import pytest

from transaction_analytics.settings import get_settings

# Port 1 refuses connections; the lifespan creates an engine but never connects.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"


@pytest.fixture
def app_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Settings for running the real lifespan without a database. Yields monkeypatch so a test
    can change a setting before the app starts; the cached settings are read again on startup."""
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
