import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BeforeValidator

from transaction_analytics.api.deps import OwnerIdDep, SessionDep
from transaction_analytics.api.schemas import (
    CurrencyTotalsOut,
    MonthlySummaryOut,
    MonthTotalsOut,
    SummaryOut,
    TransactionOut,
    TransactionPageOut,
)
from transaction_analytics.domain.analytics import MAX_CURSOR_LENGTH
from transaction_analytics.domain.fields import is_iso_date_text
from transaction_analytics.services import transactions as transaction_service

router = APIRouter(prefix="/accounts/{account_id}", tags=["transactions"])

MAX_TRANSACTIONS_PAGE_SIZE = 500


def _iso_date_only(value: object) -> object:
    # pydantic's date also accepts Unix times ("1699920000") and midnight datetimes; a query date
    # is YYYY-MM-DD only, like a statement date (ADR 0002). The calendar check stays pydantic's.
    if isinstance(value, str) and not is_iso_date_text(value):
        raise ValueError("Date must be in YYYY-MM-DD format.")
    return value


_IsoDate = Annotated[date, BeforeValidator(_iso_date_only)]
# "from" is a Python keyword, hence the alias. Both ends are inclusive; either may be left out.
FromQuery = Annotated[_IsoDate | None, Query(alias="from")]
ToQuery = Annotated[_IsoDate | None, Query()]


@router.get("/transactions")
async def list_transactions(
    account_id: uuid.UUID,
    session: SessionDep,
    owner_id: OwnerIdDep,
    start: FromQuery = None,
    to: ToQuery = None,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TRANSACTIONS_PAGE_SIZE)] = 100,
) -> TransactionPageOut:
    """The account's transactions, newest first. Pass next_cursor as cursor for the next page."""
    page = await transaction_service.list_transactions(
        session,
        owner_id=owner_id,
        account_id=account_id,
        start=start,
        end=to,
        cursor=cursor,
        limit=limit,
    )
    return TransactionPageOut(
        items=[TransactionOut.of(transaction) for transaction in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/summary")
async def get_summary(
    account_id: uuid.UUID,
    session: SessionDep,
    owner_id: OwnerIdDep,
    start: FromQuery = None,
    to: ToQuery = None,
) -> SummaryOut:
    """Inflow, outflow and net per currency. Currencies are never added together."""
    totals = await transaction_service.summarize(
        session, owner_id=owner_id, account_id=account_id, start=start, end=to
    )
    return SummaryOut(
        account_id=account_id,
        from_=start,
        to=to,
        totals=[CurrencyTotalsOut.of(currency_totals) for currency_totals in totals],
    )


@router.get("/summary/monthly")
async def get_monthly_summary(
    account_id: uuid.UUID,
    session: SessionDep,
    owner_id: OwnerIdDep,
    start: FromQuery = None,
    to: ToQuery = None,
) -> MonthlySummaryOut:
    """Per month and currency; months without transactions are left out."""
    months = await transaction_service.summarize_monthly(
        session, owner_id=owner_id, account_id=account_id, start=start, end=to
    )
    return MonthlySummaryOut(
        account_id=account_id,
        from_=start,
        to=to,
        months=[MonthTotalsOut.of(month) for month in months],
    )
