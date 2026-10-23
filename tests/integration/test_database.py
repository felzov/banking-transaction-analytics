"""Properties of the test database and of the per-test isolation itself."""

import logging
from collections.abc import AsyncIterator

import httpx2
import pytest
from factories import SEEDED_OWNERS, account
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from transaction_analytics.api.deps import SessionDep, get_session
from transaction_analytics.data.db import create_engine, create_sessionmaker
from transaction_analytics.data.errors import error_kind
from transaction_analytics.data.models import Base, User
from transaction_analytics.main import create_app
from transaction_analytics.settings import get_settings

pytestmark = pytest.mark.integration

MARKER = "LEAK-ME-5678"


@pytest.fixture
async def bounded_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine with the app's statement limit, set very low (ADR 0010)."""
    engine = create_engine(database_url, poolclass=NullPool, statement_timeout_seconds=0.1)
    yield engine
    await engine.dispose()


async def test_app_engine_cancels_a_slow_statement(bounded_engine: AsyncEngine) -> None:
    async with bounded_engine.connect() as connection:
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar_one() == "100ms"

        with pytest.raises(DBAPIError) as exc:
            await connection.execute(text("SELECT pg_sleep(1)"))
        assert error_kind(exc.value) == "QueryCanceledError"

        # Cancelled on the server: the connection is still usable after a rollback.
        await connection.rollback()
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1


async def test_the_app_lifespan_sets_the_statement_timeout(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review fix (M1): end to end, from the setting through the lifespan to the server session.
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_SECONDS", "0.25")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with app.router.lifespan_context(app), app.state.sessionmaker() as session:
            timeout = (await session.execute(text("SHOW statement_timeout"))).scalar_one()
    finally:
        get_settings.cache_clear()

    assert timeout == "250ms"


async def test_a_cancelled_statement_is_a_fixed_500(
    bounded_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app()
    sessions = create_sessionmaker(bounded_engine)

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as s:
            yield s

    @app.get("/slow")
    async def slow(s: SessionDep) -> None:
        await s.execute(text("SELECT pg_sleep(1), :marker"), {"marker": MARKER})

    app.dependency_overrides[get_session] = session
    transport = httpx2.ASGITransport(app=app)
    with caplog.at_level(logging.DEBUG):
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slow")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "QueryCanceledError" in caplog.text
    assert MARKER not in caplog.text


async def test_database_encoding_is_utf8(session: AsyncSession) -> None:
    # char_length() limits in the CHECK constraints count characters only in UTF8.
    assert (await session.execute(text("SHOW server_encoding"))).scalar_one() == "UTF8"


async def test_isolation_a_commit_inside_a_test(session: AsyncSession) -> None:
    session.add(account())
    await session.commit()  # releases a savepoint only; the outer transaction is rolled back


async def test_isolation_b_tables_are_empty_at_test_start(session: AsyncSession) -> None:
    # Runs after test_isolation_a (definition order) and after test_constraints.py (file order):
    # any row that survived an earlier test would show up here. Only the seeded owners exist.
    for table in Base.metadata.sorted_tables:
        if table.name == "users":
            continue
        count = (await session.execute(select(func.count()).select_from(table))).scalar_one()
        assert count == 0, table.name
    assert set(await session.scalars(select(User.id))) == set(SEEDED_OWNERS)
