"""The schema revision this code was written for, and the one the database is at (ADR 0010).

Not scoped by an owner: Alembic's own bookkeeping table holds no user data.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The newest migration in migrations/versions. tests/unit/test_schema_revision.py fails when a
# migration is added without updating this.
EXPECTED_SCHEMA_REVISION = "0006"


async def schema_revisions(session: AsyncSession) -> list[str]:
    """Alembic's current revisions; empty when the database was never migrated."""
    # to_regclass is NULL for a missing table, where selecting from it would abort the transaction.
    exists = await session.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
    if not exists:
        return []
    return list(await session.scalars(text("SELECT version_num FROM alembic_version")))
