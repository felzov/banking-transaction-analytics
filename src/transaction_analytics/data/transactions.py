"""Transaction reads: listing and per-currency totals (ADR 0008).

Every function requires owner_id and account_id; there is no unscoped lookup. All of them are
served by ix_transactions_owner_id_account_id_booked_on_id. They read transactions only, not
import_jobs: any transaction an account holds counts, whatever its source (Phase 7).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Numeric, Select, cast, func, select, tuple_, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TIMESTAMP

from transaction_analytics.data.models import Transaction
from transaction_analytics.domain.analytics import (
    CurrencyTotals,
    DateRange,
    MonthTotals,
    TransactionCursor,
)


def _scope(
    owner_id: uuid.UUID, account_id: uuid.UUID, dates: DateRange
) -> list[ColumnElement[bool]]:
    conditions = [Transaction.owner_id == owner_id, Transaction.account_id == account_id]
    if dates.start is not None:
        conditions.append(Transaction.booked_on >= dates.start)
    if dates.end is not None:
        conditions.append(Transaction.booked_on <= dates.end)
    return conditions


# What the listing returns (api/schemas.TransactionOut) and what its cursor needs.
_LISTED_COLUMNS = (
    Transaction.id,
    Transaction.import_job_id,
    Transaction.booked_on,
    Transaction.description,
    Transaction.currency,
    Transaction.amount_minor,
)


async def list_transactions(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    dates: DateRange,
    after: TransactionCursor | None,
    limit: int,
) -> list[Transaction]:
    """Keyset page, newest first: ordered by (booked_on, id) descending.

    id breaks ties between transactions of one day; it is random, so their order is stable but
    means nothing. The row comparison reads the index backwards from the cursor.

    Loads only the listed columns (not the fingerprint or a second copy of the description);
    reading any other attribute raises instead of lazily loading it.
    """
    statement = (
        select(Transaction)
        .options(load_only(*_LISTED_COLUMNS, raiseload=True))
        .where(*_scope(owner_id, account_id, dates))
    )
    if after is not None:
        statement = statement.where(
            tuple_(Transaction.booked_on, Transaction.id) < tuple_(after.booked_on, after.id)
        )
    statement = statement.order_by(Transaction.booked_on.desc(), Transaction.id.desc()).limit(limit)
    return list(await session.scalars(statement))


def _sum(condition: ColumnElement[bool]) -> ColumnElement[Decimal]:
    # sum(bigint) is numeric in PostgreSQL, so it cannot overflow; asyncpg returns an exact
    # Decimal, which _totals turns into an int. coalesce: a sum over no rows is NULL.
    return func.coalesce(
        type_coerce(func.sum(Transaction.amount_minor).filter(condition), Numeric()), 0
    )


_TOTAL_COLUMNS = (
    Transaction.currency,
    func.count().label("transactions"),
    _sum(Transaction.amount_minor > 0).label("inflow_minor"),
    _sum(Transaction.amount_minor < 0).label("outflow_minor"),
)


def _totals(row: Any) -> CurrencyTotals:
    return CurrencyTotals(
        currency=row.currency,
        transactions=row.transactions,
        # int() of a Decimal is exact; the values are integral sums of integers.
        inflow_minor=int(row.inflow_minor),
        outflow_minor=int(row.outflow_minor),
    )


async def currency_totals(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID, dates: DateRange
) -> list[CurrencyTotals]:
    """One row per currency the account has in the range, ordered by currency; never summed
    across currencies (ADR 0001)."""
    statement: Select[Any] = (
        select(*_TOTAL_COLUMNS)
        .where(*_scope(owner_id, account_id, dates))
        .group_by(Transaction.currency)
        .order_by(Transaction.currency)
    )
    return [_totals(row) for row in await session.execute(statement)]


async def monthly_totals(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID, dates: DateRange
) -> list[MonthTotals]:
    """One row per month and currency with transactions, ordered by month, then currency.
    Months without transactions are absent."""
    # Cast to a timestamp WITHOUT time zone first: date_trunc on a date would otherwise pick the
    # timestamptz overload and involve the session's TimeZone. Plain calendar arithmetic.
    month = cast(
        func.date_trunc("month", cast(Transaction.booked_on, TIMESTAMP(timezone=False))), Date
    ).label("month")
    statement: Select[Any] = (
        select(month, *_TOTAL_COLUMNS)
        .where(*_scope(owner_id, account_id, dates))
        .group_by(month, Transaction.currency)
        .order_by(month, Transaction.currency)
    )
    rows = await session.execute(statement)
    return [MonthTotals(month=row.month, totals=_totals(row)) for row in rows]
