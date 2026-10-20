"""Listing and per-currency totals through the service, against PostgreSQL (ADR 0008).

Rows are built with factories, not imported, so amounts and dates the CSV path rarely produces
(BIGINT extremes, zero, year 99) are easy to set up. HTTP shapes are in test_transactions_api.py.
"""

import random
import uuid
from collections import defaultdict
from datetime import date, timedelta

import factories
import pytest
from factories import OTHER_OWNER_ID, OWNER_ID
from sqlalchemy import inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import Account, Transaction
from transaction_analytics.domain.analytics import CurrencyTotals, MonthTotals
from transaction_analytics.domain.money import MAX_ABS_AMOUNT_MINOR, Currency
from transaction_analytics.services import transactions as transaction_service
from transaction_analytics.services.errors import AccountNotFoundError

pytestmark = pytest.mark.integration

GBP, EUR, USD, JPY = Currency.GBP, Currency.EUR, Currency.USD, Currency.JPY

Row = tuple[date, int, Currency]


async def seed(session: AsyncSession, rows: list[Row], owner_id: uuid.UUID = OWNER_ID) -> Account:
    """An account holding `rows`, all from one completed import."""
    account = factories.account(owner_id)
    job = factories.completed(account, total_rows=len(rows), valid_rows=len(rows), invalid_rows=0)
    session.add_all([account, job])
    await session.flush()
    session.add_all(
        factories.transaction(
            job, row_number=number, booked_on=booked_on, amount_minor=amount, currency=currency
        )
        for number, (booked_on, amount, currency) in enumerate(rows, start=2)
    )
    await session.flush()
    return account


async def summary(
    session: AsyncSession,
    account: Account,
    start: date | None = None,
    end: date | None = None,
    owner_id: uuid.UUID = OWNER_ID,
) -> list[CurrencyTotals]:
    return await transaction_service.summarize(
        session, owner_id=owner_id, account_id=account.id, start=start, end=end
    )


async def monthly(
    session: AsyncSession, account: Account, start: date | None = None, end: date | None = None
) -> list[MonthTotals]:
    return await transaction_service.summarize_monthly(
        session, owner_id=OWNER_ID, account_id=account.id, start=start, end=end
    )


def totals(currency: Currency, count: int, inflow: int, outflow: int) -> CurrencyTotals:
    return CurrencyTotals(
        currency=currency, transactions=count, inflow_minor=inflow, outflow_minor=outflow
    )


# --- summary -------------------------------------------------------------------------------------


async def test_totals_are_per_currency_and_never_mixed(session: AsyncSession) -> None:
    day = date(2026, 9, 1)
    account = await seed(
        session,
        [
            (day, 250_000, GBP),
            (day, -320, GBP),
            (day, -95_000, GBP),
            (day, -12_050, EUR),
            (day, 1_000, EUR),
            (day, -1_500, JPY),
        ],
    )

    result = await summary(session, account)

    assert result == [
        totals(EUR, 2, 1_000, -12_050),
        totals(GBP, 3, 250_000, -95_320),
        totals(JPY, 1, 0, -1_500),
    ]
    assert [t.net_minor for t in result] == [-11_050, 154_680, -1_500]


async def test_zero_amount_is_counted_but_in_neither_sum(session: AsyncSession) -> None:
    account = await seed(session, [(date(2026, 9, 1), 0, GBP), (date(2026, 9, 2), 0, GBP)])

    assert await summary(session, account) == [totals(GBP, 2, 0, 0)]


async def test_sums_beyond_bigint_are_exact(session: AsyncSession) -> None:
    # A single amount fits BIGINT (ADR 0001); a sum of two does not. sum(bigint) is numeric.
    day = date(2026, 9, 1)
    account = await seed(
        session,
        [
            (day, MAX_ABS_AMOUNT_MINOR, USD),
            (day, MAX_ABS_AMOUNT_MINOR, USD),
            (day, -MAX_ABS_AMOUNT_MINOR, USD),
            (day, -MAX_ABS_AMOUNT_MINOR, USD),
            (day, -1, USD),
        ],
    )

    (result,) = await summary(session, account)

    assert result.inflow_minor == 2 * MAX_ABS_AMOUNT_MINOR
    assert result.outflow_minor == -2 * MAX_ABS_AMOUNT_MINOR - 1
    assert result.net_minor == -1
    assert type(result.inflow_minor) is int and type(result.outflow_minor) is int


async def test_date_range_is_inclusive_at_both_ends(session: AsyncSession) -> None:
    account = await seed(
        session,
        [
            (date(2026, 8, 31), -1, GBP),
            (date(2026, 9, 1), -10, GBP),
            (date(2026, 9, 30), -100, GBP),
            (date(2026, 10, 1), -1_000, GBP),
        ],
    )
    sep_1, sep_30 = date(2026, 9, 1), date(2026, 9, 30)

    assert await summary(session, account, sep_1, sep_30) == [totals(GBP, 2, 0, -110)]
    assert await summary(session, account, sep_1, sep_1) == [totals(GBP, 1, 0, -10)]
    assert await summary(session, account, start=sep_1) == [totals(GBP, 3, 0, -1_110)]
    assert await summary(session, account, end=sep_30) == [totals(GBP, 3, 0, -111)]
    assert await summary(session, account) == [totals(GBP, 4, 0, -1_111)]


async def test_no_transactions_means_no_rows_not_zeros(session: AsyncSession) -> None:
    empty = await seed(session, [])
    september = await seed(session, [(date(2026, 9, 1), -10, GBP)])

    assert await summary(session, empty) == []
    assert await summary(session, september, start=date(2026, 10, 1)) == []
    assert await monthly(session, empty) == []


async def test_other_accounts_are_not_included(session: AsyncSession) -> None:
    day = date(2026, 9, 1)
    account = await seed(session, [(day, -10, GBP)])
    await seed(session, [(day, -20, GBP)])  # the same owner's other account
    await seed(session, [(day, -40, GBP)], owner_id=OTHER_OWNER_ID)

    assert await summary(session, account) == [totals(GBP, 1, 0, -10)]
    assert await monthly(session, account) == [
        MonthTotals(month=date(2026, 9, 1), totals=totals(GBP, 1, 0, -10))
    ]
    page = await list_page(session, account, limit=10)
    assert [t.amount_minor for t in page.items] == [-10]


async def test_another_owners_account_is_not_found(session: AsyncSession) -> None:
    foreign = await seed(session, [(date(2026, 9, 1), -40, GBP)], owner_id=OTHER_OWNER_ID)
    scope = {"owner_id": OWNER_ID, "account_id": foreign.id, "start": None, "end": None}

    with pytest.raises(AccountNotFoundError):
        await transaction_service.summarize(session, **scope)
    with pytest.raises(AccountNotFoundError):
        await transaction_service.summarize_monthly(session, **scope)
    with pytest.raises(AccountNotFoundError):
        await transaction_service.list_transactions(session, **scope, cursor=None, limit=10)


# --- monthly -------------------------------------------------------------------------------------


async def test_months_split_at_calendar_boundaries(session: AsyncSession) -> None:
    account = await seed(
        session,
        [
            (date(2026, 1, 31), -1, GBP),
            (date(2026, 2, 1), -2, GBP),
            (date(2026, 12, 31), -4, GBP),
            (date(2027, 1, 1), -8, GBP),
            (date(2028, 2, 29), 16, GBP),  # leap day
            (date(2028, 2, 29), -32, EUR),  # same month, other currency: its own row
            (date(99, 1, 15), -64, JPY),  # year 99 (ADR 0003 stores it; the parser accepts it)
        ],
    )

    result = await monthly(session, account)

    assert result == [
        MonthTotals(month=date(99, 1, 1), totals=totals(JPY, 1, 0, -64)),
        MonthTotals(month=date(2026, 1, 1), totals=totals(GBP, 1, 0, -1)),
        MonthTotals(month=date(2026, 2, 1), totals=totals(GBP, 1, 0, -2)),
        MonthTotals(month=date(2026, 12, 1), totals=totals(GBP, 1, 0, -4)),
        MonthTotals(month=date(2027, 1, 1), totals=totals(GBP, 1, 0, -8)),
        MonthTotals(month=date(2028, 2, 1), totals=totals(EUR, 1, 0, -32)),
        MonthTotals(month=date(2028, 2, 1), totals=totals(GBP, 1, 16, 0)),
    ]  # months without transactions (March 2026 ...) are absent


async def test_monthly_respects_the_date_range(session: AsyncSession) -> None:
    account = await seed(
        session,
        [(date(2026, 9, 14), -1, GBP), (date(2026, 9, 15), -2, GBP), (date(2026, 10, 1), -4, GBP)],
    )

    # The range cuts September in half: its month row only counts the days inside the range.
    result = await monthly(session, account, start=date(2026, 9, 15), end=date(2026, 9, 30))

    assert result == [MonthTotals(month=date(2026, 9, 1), totals=totals(GBP, 1, 0, -2))]


async def test_summary_and_monthly_match_an_independent_computation(
    session: AsyncSession,
) -> None:
    # A seeded random mix of currencies, signs and dates, checked against plain Python sums.
    generator = random.Random(6)
    rows: list[Row] = [
        (
            date(2025, 11, 1) + timedelta(days=generator.randint(0, 120)),
            generator.choice([0, generator.randint(-500_000, 500_000)]),
            generator.choice(list(Currency)),
        )
        for _ in range(300)
    ]
    account = await seed(session, rows)
    start, end = date(2025, 12, 10), date(2026, 2, 20)

    expected: dict[tuple[date, Currency], list[int]] = defaultdict(lambda: [0, 0, 0])
    for booked_on, amount, currency in rows:
        if start <= booked_on <= end:
            bucket = expected[(booked_on.replace(day=1), currency)]
            bucket[0] += 1
            bucket[1] += max(amount, 0)
            bucket[2] += min(amount, 0)
    expected_monthly = [
        MonthTotals(month=month, totals=totals(currency, *bucket))
        for (month, currency), bucket in sorted(expected.items())
    ]
    by_currency: dict[Currency, list[int]] = defaultdict(lambda: [0, 0, 0])
    for (_, currency), bucket in expected.items():
        by_currency[currency] = [a + b for a, b in zip(by_currency[currency], bucket, strict=True)]
    expected_summary = [totals(c, *by_currency[c]) for c in sorted(by_currency)]

    assert await monthly(session, account, start, end) == expected_monthly
    assert await summary(session, account, start, end) == expected_summary


# --- listing -------------------------------------------------------------------------------------


async def list_page(
    session: AsyncSession,
    account: Account,
    *,
    cursor: str | None = None,
    limit: int = 3,
    start: date | None = None,
    end: date | None = None,
) -> transaction_service.TransactionPage:
    return await transaction_service.list_transactions(
        session,
        owner_id=OWNER_ID,
        account_id=account.id,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
    )


async def all_pages(
    session: AsyncSession,
    account: Account,
    *,
    limit: int,
    start: date | None = None,
    end: date | None = None,
) -> list[list[Transaction]]:
    pages = []
    cursor = None
    while True:
        page = await list_page(session, account, cursor=cursor, limit=limit, start=start, end=end)
        pages.append(page.items)
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor


async def test_listing_is_newest_first(session: AsyncSession) -> None:
    account = await seed(
        session,
        [(date(2026, 9, 2), -2, GBP), (date(2026, 9, 3), -3, GBP), (date(2026, 9, 1), -1, GBP)],
    )

    page = await list_page(session, account, limit=10)

    assert [t.booked_on.day for t in page.items] == [3, 2, 1]
    assert page.next_cursor is None


async def test_pages_over_ties_have_no_gaps_or_repeats(session: AsyncSession) -> None:
    # Seven transactions on one day: the cursor must break the tie by id, or a page boundary
    # inside the day would repeat or skip rows.
    day = date(2026, 9, 1)
    account = await seed(
        session, [(day - timedelta(days=1), -100, GBP)] + [(day, -n, GBP) for n in range(1, 8)]
    )

    pages = await all_pages(session, account, limit=3)

    assert [len(page) for page in pages] == [3, 3, 2]
    ids = [t.id for page in pages for t in page]
    assert len(set(ids)) == 8
    listed = [(t.booked_on, t.id) for page in pages for t in page]
    assert listed == sorted(listed, reverse=True)
    assert pages[-1][-1].amount_minor == -100  # the older day comes last


async def test_exact_multiple_of_the_page_size_ends_without_an_empty_page(
    session: AsyncSession,
) -> None:
    account = await seed(session, [(date(2026, 9, n), -n, GBP) for n in range(1, 7)])

    pages = await all_pages(session, account, limit=3)

    assert [len(page) for page in pages] == [3, 3]


async def test_listing_with_a_date_range_and_cursor(session: AsyncSession) -> None:
    account = await seed(session, [(date(2026, 9, n), -n, GBP) for n in range(1, 11)])

    pages = await all_pages(session, account, limit=2, start=date(2026, 9, 3), end=date(2026, 9, 7))

    assert [[t.booked_on.day for t in page] for page in pages] == [[7, 6], [5, 4], [3]]


async def test_empty_account_lists_nothing(session: AsyncSession) -> None:
    page = await list_page(session, await seed(session, []))

    assert page.items == []
    assert page.next_cursor is None


async def test_listing_loads_only_what_the_response_shows(session: AsyncSession) -> None:
    # Review fix: whole rows were loaded, including a second copy of every description
    # (normalized_description) and the 32-byte fingerprint, for up to 500 rows per page.
    account = await seed(session, [(date(2026, 9, 1), -10, GBP)])
    session.expunge_all()  # seeded objects are fully loaded; the listing must load its own

    (listed,) = (await list_page(session, account, limit=10)).items

    assert {"fingerprint", "normalized_description", "occurrence", "created_at"} <= set(
        inspect(listed).unloaded
    )
    with pytest.raises(InvalidRequestError):  # raises instead of a lazy load (fails in async)
        _ = listed.fingerprint
    assert (listed.booked_on, listed.amount_minor, listed.currency) == (date(2026, 9, 1), -10, GBP)
