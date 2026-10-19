"""Properties of the test database and of the per-test isolation itself."""

import pytest
from factories import SEEDED_OWNERS, account
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import Base, User

pytestmark = pytest.mark.integration


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
