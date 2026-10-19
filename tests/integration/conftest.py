"""Fixtures for tests against a real PostgreSQL (ADR 0003, "Test isolation").

- The database comes from TEST_DATABASE_URL (env or .env), never DATABASE_URL, and its name must
  end in "_test": it is dropped and recreated at the start of every test session.
- Each test runs in one outer transaction that is rolled back; the session uses savepoints, so
  test code may flush, commit and catch IntegrityError without leaking rows into other tests.
- Engines use NullPool and live for one test, so no connection outlives its event loop.
- Every migrated database gets committed users rows for the fixed test owners (factories.py), so
  tests can create their accounts directly. Tests of authentication create users of their own.
- API tests call the app in-process through httpx.ASGITransport, on the test's event loop, with
  the request session replaced by the test session. starlette's TestClient would run the app on
  another event loop, where the asyncpg connection of the test session cannot be used.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from factories import OWNER_ID, SEEDED_OWNERS
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from transaction_analytics.api.deps import get_import_limits, get_owner_id, get_session
from transaction_analytics.data.db import create_engine
from transaction_analytics.domain.csv_parser import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS
from transaction_analytics.main import create_app
from transaction_analytics.services.imports import ImportLimits

ROOT = Path(__file__).resolve().parents[2]


class _TestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    test_database_url: SecretStr | None = None


def _test_database_url() -> URL:
    raw = _TestSettings().test_database_url
    if raw is None:
        pytest.fail(
            "TEST_DATABASE_URL is not set. Copy .env.example to .env and run `make db-up`.",
            pytrace=False,
        )
    url = make_url(raw.get_secret_value())
    if not (url.database or "").endswith("_test"):
        pytest.fail(
            "TEST_DATABASE_URL must name a database ending in '_test'; it is dropped on each run.",
            pytrace=False,
        )
    return url


def _url_string(url: URL) -> str:
    return url.render_as_string(hide_password=False)


async def _recreate_database(url: URL) -> None:
    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT on the
    # maintenance database.
    engine = create_engine(_url_string(url.set(database="postgres")), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            name = url.database
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            # Explicit UTF8: char_length() limits count characters only in UTF8 (in SQL_ASCII they
            # count bytes). template0, because template1 may have a different encoding.
            await connection.execute(
                text(f"CREATE DATABASE \"{name}\" ENCODING 'UTF8' TEMPLATE template0")
            )
    finally:
        await engine.dispose()


async def _drop_database(url: URL) -> None:
    engine = create_engine(_url_string(url.set(database="postgres")), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


async def _seed_owners(url: URL) -> None:
    engine = create_engine(_url_string(url), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO users (id, name) VALUES (:id, :name)"),
                [{"id": owner_id, "name": name} for owner_id, name in SEEDED_OWNERS.items()],
            )
    finally:
        await engine.dispose()


def _migrate_and_seed(url: URL) -> None:
    command.upgrade(_alembic_config(url), "head")
    asyncio.run(_seed_owners(url))


def _alembic_config(url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = _url_string(url)
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    """A freshly created, fully migrated test database (clean DB -> upgrade head)."""
    url = _test_database_url()
    asyncio.run(_recreate_database(url))
    _migrate_and_seed(url)
    # Left in place after the run so a failure can be inspected; recreated next session.
    return _url_string(url)


@pytest.fixture
def empty_database_url() -> Iterator[URL]:
    """A separate, empty database for migration tests (downgrade would break the shared one)."""
    base = _test_database_url()
    url = base.set(database=base.database.removesuffix("_test") + "_migrations_test")
    asyncio.run(_recreate_database(url))
    yield url
    asyncio.run(_drop_database(url))


@pytest.fixture
def migration_config(empty_database_url: URL) -> Config:
    return _alembic_config(empty_database_url)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        outer = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()


@pytest.fixture
def app(session: AsyncSession) -> FastAPI:
    """The real app, acting as OWNER_ID with default limits. Tests may change the overrides.

    Authentication is overridden here; tests/integration/test_auth_api.py removes the override.
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_owner_id] = lambda: OWNER_ID
    app.dependency_overrides[get_import_limits] = lambda: ImportLimits(
        max_bytes=DEFAULT_MAX_BYTES, max_rows=DEFAULT_MAX_ROWS
    )
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # raise_app_exceptions (default True): an exception escaping the app fails the test, which is
    # what the server would otherwise log with its full text.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def committed_database_url(empty_database_url: URL) -> str:
    """A separate, migrated database for tests that need real COMMITs between sessions.

    The shared database isolates tests by rolling back one outer transaction, so concurrency and
    "visible after commit" cannot be tested there. Synchronous: alembic runs asyncio.run().
    """
    _migrate_and_seed(empty_database_url)
    return _url_string(empty_database_url)
