"""Account persistence. Every function requires owner_id; there is no unscoped lookup."""

import uuid

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import Account


async def add_account(session: AsyncSession, *, owner_id: uuid.UUID, name: str) -> Account:
    # INSERT ... RETURNING loads server defaults (id, created_at) without a second query.
    statement = insert(Account).values(owner_id=owner_id, name=name).returning(Account)
    return (await session.scalars(statement)).one()


async def get_account(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID
) -> Account | None:
    statement = select(Account).where(Account.owner_id == owner_id, Account.id == account_id)
    return await session.scalar(statement)


async def list_accounts(session: AsyncSession, *, owner_id: uuid.UUID) -> list[Account]:
    statement = (
        select(Account).where(Account.owner_id == owner_id).order_by(Account.created_at, Account.id)
    )
    return list(await session.scalars(statement))
