"""Valid data survives a write and read unchanged, including the boundary values."""

from datetime import date
from pathlib import Path

import pytest
from factories import (
    OWNER_ID,
    STARTED,
    account,
    completed,
    failed,
    job,
    row_error,
    sha256_hex,
    transaction,
)
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import Account, ImportJob, ImportRowError, Transaction
from transaction_analytics.domain.csv_parser import ParseResult, parse_statement_csv
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.fields import clean_description, normalize_description
from transaction_analytics.domain.imports import ImportStatus
from transaction_analytics.domain.money import MAX_ABS_AMOUNT_MINOR, Currency

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures"


async def test_server_defaults(session: AsyncSession) -> None:
    acc = Account(owner_id=OWNER_ID, name="Current account")  # no id: the database assigns it
    session.add(acc)
    await session.flush()
    # Only the required columns: status and counts come from server defaults.
    pending = ImportJob(owner_id=OWNER_ID, account_id=acc.id, file_sha256=sha256_hex())
    session.add(pending)
    await session.flush()
    await session.refresh(acc)
    await session.refresh(pending)

    assert acc.id is not None
    assert acc.created_at.tzinfo is not None
    assert pending.status is ImportStatus.PENDING
    assert (pending.total_rows, pending.valid_rows, pending.invalid_rows) == (0, 0, 0)


async def _store(session: AsyncSession, parsed: ParseResult) -> None:
    """Persist a parse result the way the Phase 3 import service is expected to."""
    acc = account()
    session.add(acc)
    await session.flush()
    import_job = completed(
        acc,
        total_rows=parsed.total_rows,
        valid_rows=len(parsed.transactions),
        invalid_rows=len(parsed.errors),
    )
    session.add(import_job)
    await session.flush()
    session.add_all(
        [
            transaction(
                import_job,
                row_number=t.row_number,
                booked_on=t.booked_on,
                description=t.description,
                normalized_description=t.normalized_description,
                amount_minor=t.amount_minor,
                currency=t.currency,
            )
            for t in parsed.transactions
        ]
        + [
            row_error(
                import_job,
                row_number=e.row_number,
                code=e.code,
                column_name=e.column,
                message=e.message,
            )
            for e in parsed.errors
        ]
    )
    await session.commit()
    session.expunge_all()


async def test_parsed_statement_round_trips(session: AsyncSession) -> None:
    parsed = parse_statement_csv((FIXTURES / "statement_mixed_errors.csv").read_bytes())

    await _store(session, parsed)

    stored = (await session.scalars(select(Transaction).order_by(Transaction.row_number))).all()
    errors = (
        await session.scalars(select(ImportRowError).order_by(ImportRowError.row_number))
    ).all()
    assert [
        (
            t.row_number,
            t.booked_on,
            t.description,
            t.normalized_description,
            t.amount_minor,
            t.currency,
        )
        for t in stored
    ] == [
        (
            t.row_number,
            t.booked_on,
            t.description,
            t.normalized_description,
            t.amount_minor,
            t.currency,
        )
        for t in parsed.transactions
    ]
    assert all(type(t.currency) is Currency for t in stored)
    assert [(e.row_number, e.code, e.column_name, e.message) for e in errors] == [
        (e.row_number, e.code, e.column, e.message) for e in parsed.errors
    ]


async def test_nul_in_description_becomes_a_row_error_and_never_reaches_the_database(
    session: AsyncSession,
) -> None:
    content = (
        b"date,description,amount,currency\n"
        b"2026-09-01,Caf\x00e,-1.00,GBP\n"
        b"2026-09-02,Bus,-2.40,GBP\n"
    )

    await _store(session, parse_statement_csv(content))

    stored = (await session.scalars(select(Transaction))).all()
    error = (await session.scalars(select(ImportRowError))).one()
    assert [(t.row_number, t.description) for t in stored] == [(3, "Bus")]
    assert (error.row_number, error.code, error.column_name) == (
        2,
        FieldErrorCode.INVALID_CHARACTERS,
        "description",
    )


async def test_postgres_cannot_store_nul(session: AsyncSession) -> None:
    # Why the domain must reject NUL: the database refuses it with a non-integrity error that
    # constraint-name mapping would not catch.
    acc = account()
    session.add(acc)
    await session.flush()
    import_job = completed(acc)
    session.add(import_job)
    await session.flush()

    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            session.add(
                transaction(import_job, description="Caf\x00e", normalized_description="caf\x00e")
            )
            await session.flush()

    assert type(exc.value.orig.__cause__).__name__ == "CharacterNotInRepertoireError"


@pytest.mark.parametrize("amount_minor", [MAX_ABS_AMOUNT_MINOR, -MAX_ABS_AMOUNT_MINOR, 0])
async def test_amount_boundaries_are_stored_exactly(
    session: AsyncSession, amount_minor: int
) -> None:
    acc = account()
    session.add(acc)
    await session.flush()
    import_job = completed(acc)
    session.add(import_job)
    await session.flush()
    session.add(transaction(import_job, amount_minor=amount_minor, currency=Currency.JPY))
    await session.commit()
    session.expunge_all()

    assert (await session.scalars(select(Transaction.amount_minor))).one() == amount_minor


async def test_longest_valid_descriptions_fit(session: AsyncSession) -> None:
    # casefold() turns each "ΐ" into three code points: 500 characters become 1500.
    description = clean_description("ΐ" * 500)
    normalized = normalize_description(description)
    assert (len(description), len(normalized)) == (500, 1500)

    acc = account()
    session.add(acc)
    await session.flush()
    import_job = completed(acc)
    session.add(import_job)
    await session.flush()
    session.add(
        transaction(
            import_job,
            booked_on=date(2026, 2, 28),
            description=description,
            normalized_description=normalized,
        )
    )
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(Transaction))).one()
    assert (stored.description, stored.normalized_description) == (description, normalized)


async def test_every_status_with_a_consistent_lifecycle_is_accepted(
    session: AsyncSession,
) -> None:
    acc = account()
    session.add(acc)
    await session.flush()
    session.add_all(
        [
            job(acc, file_sha256=sha256_hex(1)),
            job(
                acc,
                file_sha256=sha256_hex(2),
                status=ImportStatus.PROCESSING,
                started_at=STARTED,
            ),
            completed(acc, file_sha256=sha256_hex(3)),
            failed(acc, file_sha256=sha256_hex(4)),
        ]
    )
    await session.flush()

    statuses = (await session.scalars(select(ImportJob.status))).all()
    assert sorted(statuses) == sorted(ImportStatus)
