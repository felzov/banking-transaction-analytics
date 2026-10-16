"""Fixtures for tests against a real PostgreSQL (ADR 0003, "Test isolation").

- The database comes from TEST_DATABASE_URL (env or .env), never DATABASE_URL, and its name must
  end in "_test": it is dropped and recreated at the start of every test session.
- Each test runs in one outer transaction that is rolled back; the session uses savepoints, so
  test code may flush, commit and catch IntegrityError without leaking rows into other tests.
- Engines use NullPool and live for one test, so no connection outlives its event loop.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from transaction_analytics.data.db import create_engine

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


def _alembic_config(url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = _url_string(url)
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    """A freshly created, fully migrated test database (clean DB -> upgrade head)."""
    url = _test_database_url()
    asyncio.run(_recreate_database(url))
    command.upgrade(_alembic_config(url), "head")
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
