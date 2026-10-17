import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data.models import Account
from transaction_analytics.domain.accounts import clean_account_name
from transaction_analytics.services.errors import AccountNotFoundError


async def create_account(session: AsyncSession, *, owner_id: uuid.UUID, name: str) -> Account:
    """Raises AccountValidationError for an invalid name."""
    account = await accounts_data.add_account(
        session, owner_id=owner_id, name=clean_account_name(name)
    )
    await session.commit()
    return account


async def get_account(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    account = await accounts_data.get_account(session, owner_id=owner_id, account_id=account_id)
    if account is None:
        raise AccountNotFoundError
    return account


async def list_accounts(session: AsyncSession, *, owner_id: uuid.UUID) -> list[Account]:
    return await accounts_data.list_accounts(session, owner_id=owner_id)
