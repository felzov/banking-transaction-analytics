"""GET /ready against a real database (ADR 0010).

Schema changes happen inside the test's outer transaction, which is rolled back (DDL included:
PostgreSQL's DDL is transactional), so the shared database keeps its alembic_version.
"""

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.schema import EXPECTED_SCHEMA_REVISION, schema_revisions

pytestmark = pytest.mark.integration


async def test_a_migrated_database_is_ready(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    assert await schema_revisions(session) == [EXPECTED_SCHEMA_REVISION]

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_a_database_behind_the_code_is_not_ready(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    await session.execute(text("UPDATE alembic_version SET version_num = '0005'"))

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"


async def test_two_heads_are_not_ready(client: httpx2.AsyncClient, session: AsyncSession) -> None:
    await session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('9999')"))

    response = await client.get("/ready")

    assert response.status_code == 503


async def test_a_database_never_migrated_is_not_ready(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    await session.execute(text("DROP TABLE alembic_version"))

    assert await schema_revisions(session) == []  # no error: the transaction stays usable
    response = await client.get("/ready")

    assert response.status_code == 503
