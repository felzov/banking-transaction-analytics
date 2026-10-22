import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data.errors import constraint_name
from transaction_analytics.data.models import LEDGER_LINK_CONSTRAINT, Account
from transaction_analytics.domain.accounts import clean_account_name
from transaction_analytics.domain.ledger import LedgerLink
from transaction_analytics.services.errors import (
    AccountNotFoundError,
    LedgerAccountAlreadyLinkedError,
)


async def create_account(
    session: AsyncSession, *, owner_id: uuid.UUID, name: str, ledger: LedgerLink | None = None
) -> Account:
    """A CSV account, or with `ledger` an account fed by that ledger account (ADR 0009).

    The link is not checked against the ledger here: that needs the owner's ledger token, which
    only a sync carries, and the ledger enforces it on every sync.

    Raises AccountValidationError for an invalid name, LedgerAccountAlreadyLinkedError when the
    owner has linked that ledger account before.
    """
    try:
        # A savepoint, so the session stays usable after the violation it maps.
        async with session.begin_nested():
            account = await accounts_data.add_account(
                session,
                owner_id=owner_id,
                name=clean_account_name(name),
                ledger=ledger,
            )
    except IntegrityError as exc:
        if constraint_name(exc) != LEDGER_LINK_CONSTRAINT:
            raise
        raise LedgerAccountAlreadyLinkedError from None
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
