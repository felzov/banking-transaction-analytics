"""Listing and summarizing an account's transactions (ADR 0008).

Query parameters are validated before the database is touched; then the account must belong to
the owner (404 otherwise, like every other resource), then one read query runs.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import transactions as transactions_data
from transaction_analytics.data.models import Transaction
from transaction_analytics.domain.analytics import (
    CurrencyTotals,
    DateRange,
    MonthTotals,
    TransactionCursor,
)
from transaction_analytics.services.accounts import get_account


@dataclass(frozen=True, slots=True)
class TransactionPage:
    items: list[Transaction]
    # Pass as cursor to get the next page; None when this is the last page.
    next_cursor: str | None


async def list_transactions(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    start: date | None,
    end: date | None,
    cursor: str | None,
    limit: int,
) -> TransactionPage:
    """Raises AnalyticsValidationError or AccountNotFoundError."""
    dates = DateRange(start=start, end=end)
    after = None if cursor is None else TransactionCursor.decode(cursor)
    await get_account(session, owner_id=owner_id, account_id=account_id)
    # One extra row tells whether another page exists without a COUNT query.
    rows = await transactions_data.list_transactions(
        session, owner_id=owner_id, account_id=account_id, dates=dates, after=after, limit=limit + 1
    )
    if len(rows) > limit:
        last = rows[limit - 1]
        return TransactionPage(
            items=rows[:limit],
            next_cursor=TransactionCursor(booked_on=last.booked_on, id=last.id).encode(),
        )
    return TransactionPage(items=rows, next_cursor=None)


async def summarize(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    start: date | None,
    end: date | None,
) -> list[CurrencyTotals]:
    """Raises AnalyticsValidationError or AccountNotFoundError."""
    dates = DateRange(start=start, end=end)
    await get_account(session, owner_id=owner_id, account_id=account_id)
    return await transactions_data.currency_totals(
        session, owner_id=owner_id, account_id=account_id, dates=dates
    )


async def summarize_monthly(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    start: date | None,
    end: date | None,
) -> list[MonthTotals]:
    """Raises AnalyticsValidationError or AccountNotFoundError."""
    dates = DateRange(start=start, end=end)
    await get_account(session, owner_id=owner_id, account_id=account_id)
    return await transactions_data.monthly_totals(
        session, owner_id=owner_id, account_id=account_id, dates=dates
    )
