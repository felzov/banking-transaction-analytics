"""Import behaviour that needs real COMMITs and separate sessions (concurrency, pool use).

Runs on its own throwaway database (committed_database_url), not inside the rollback-isolated
shared one, so what one session commits is really visible to another.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
from factories import OWNER_ID
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data import imports as imports_data
from transaction_analytics.data.db import create_engine, create_sessionmaker
from transaction_analytics.data.models import ImportJob, Transaction
from transaction_analytics.domain.csv_parser import (
    ParsedTransaction,
    ParseResult,
    parse_statement_csv,
)
from transaction_analytics.domain.dedup import assign_occurrences
from transaction_analytics.domain.imports import (
    ImportFailureCode,
    ImportOutcome,
    ImportStatus,
    completed_outcome,
)
from transaction_analytics.domain.money import Currency
from transaction_analytics.services import imports as import_service
from transaction_analytics.services.errors import DuplicateImportError, ImportProcessingError
from transaction_analytics.services.imports import ImportLimits

pytestmark = pytest.mark.integration

VALID = (Path(__file__).parents[1] / "fixtures" / "statement_valid.csv").read_bytes()
LIMITS = ImportLimits(max_bytes=5 * 1024 * 1024, max_rows=50_000)


@pytest.fixture
async def scratch_engine(committed_database_url: str) -> AsyncIterator[AsyncEngine]:
    # A real QueuePool (not NullPool), so checked-out connections can be observed.
    engine = create_engine(committed_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(scratch_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(scratch_engine)


@pytest.fixture
async def account_id(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        account = await accounts_data.add_account(session, owner_id=OWNER_ID, name="Current")
        await session.commit()
        return account.id


async def _wait_until_a_query_waits_on_a_lock(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        for _ in range(500):
            waiting = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            if waiting:
                return
            await asyncio.sleep(0.01)
    pytest.fail("the second upload never waited on the unique index")


async def test_concurrent_upload_of_the_same_file_is_a_conflict(
    scratch_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    # Forces the race: the first import holds the unique index uncommitted while the second one
    # tries to insert the same file. The second must wait, then see the commit and report it.
    async with sessions() as first, sessions() as second:
        held = await imports_data.insert_job(
            first,
            owner_id=OWNER_ID,
            account_id=account_id,
            file_sha256=hashlib.sha256(VALID).hexdigest(),
            outcome=ImportOutcome(status=ImportStatus.COMPLETED),
        )
        racing = asyncio.create_task(
            import_service.import_statement(
                second, owner_id=OWNER_ID, account_id=account_id, content=VALID, limits=LIMITS
            )
        )
        await _wait_until_a_query_waits_on_a_lock(scratch_engine)
        assert not racing.done()

        await first.commit()
        with pytest.raises(DuplicateImportError) as exc:
            await racing

    assert exc.value.existing_import_id == held.id
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ImportJob)) == 1


async def test_internal_error_is_recorded_with_a_real_commit(
    sessions: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def parser_letting_an_out_of_range_amount_through(content: bytes, **limits: int) -> ParseResult:
        row = ParsedTransaction(
            row_number=2,
            booked_on=date(2026, 9, 1),
            description="Payment",
            normalized_description="payment",
            amount_minor=2**63,
            currency=Currency.GBP,
        )
        return ParseResult(transactions=[row])

    monkeypatch.setattr(
        import_service, "parse_statement_csv", parser_letting_an_out_of_range_amount_through
    )

    async with sessions() as session:
        with pytest.raises(ImportProcessingError) as exc:
            await import_service.import_statement(
                session, owner_id=OWNER_ID, account_id=account_id, content=VALID, limits=LIMITS
            )

    async with sessions() as session:  # a new session sees only what was committed
        job = await session.get(ImportJob, exc.value.failed_import_id)
        assert job is not None
        assert job.status is ImportStatus.FAILED
        assert job.error_code is ImportFailureCode.INTERNAL_ERROR
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 0


async def test_no_pooled_connection_is_held_while_parsing(
    scratch_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_out: list[int] = []
    real_parser = import_service.parse_statement_csv

    def recording_parser(content: bytes, **limits: int) -> ParseResult:
        checked_out.append(scratch_engine.sync_engine.pool.checkedout())
        return real_parser(content, **limits)

    monkeypatch.setattr(import_service, "parse_statement_csv", recording_parser)

    async with sessions() as session:
        await import_service.import_statement(
            session, owner_id=OWNER_ID, account_id=account_id, content=VALID, limits=LIMITS
        )

    assert checked_out == [0]


HEADER = b"date,description,amount,currency\n"
RENT = b"2026-09-01,Rent,-950.00,GBP\n"
COFFEE = b"2026-09-03,Coffee Shop,-3.20,GBP\n"
BOOKS = b"2026-09-04,Books,-12.00,GBP\n"


async def _hold_uncommitted_import(
    session: AsyncSession, account_id: uuid.UUID, content: bytes
) -> None:
    """The first import's writes, left uncommitted so a concurrent import has to wait on them."""
    result = parse_statement_csv(content)
    job = await imports_data.insert_job(
        session,
        owner_id=OWNER_ID,
        account_id=account_id,
        file_sha256=hashlib.sha256(content).hexdigest(),
        outcome=completed_outcome(result),
    )
    await imports_data.insert_transactions(
        session, job=job, rows=assign_occurrences(result.transactions)
    )


@pytest.mark.parametrize(("first_commits", "expected_duplicates"), [(True, 1), (False, 0)])
async def test_concurrent_overlapping_imports_store_each_row_once(
    scratch_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    first_commits: bool,
    expected_duplicates: int,
) -> None:
    # Two different files (no file-level conflict) share the Coffee row. The second import must
    # wait for the first's uncommitted Coffee, then skip it if the first commits, or insert it if
    # the first rolls back. A check-then-insert would store Coffee twice here.
    async with sessions() as first, sessions() as second:
        await _hold_uncommitted_import(first, account_id, HEADER + RENT + COFFEE)
        racing = asyncio.create_task(
            import_service.import_statement(
                second,
                owner_id=OWNER_ID,
                account_id=account_id,
                content=HEADER + COFFEE + BOOKS,
                limits=LIMITS,
            )
        )
        await _wait_until_a_query_waits_on_a_lock(scratch_engine)
        assert not racing.done()

        await (first.commit() if first_commits else first.rollback())
        result = await racing

    assert result.job.duplicate_rows == expected_duplicates
    async with sessions() as session:
        coffees = await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.normalized_description == "coffee shop")
        )
        assert coffees == 1
        stored = await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.import_job_id == result.job.id)
        )
        assert stored == result.job.valid_rows - result.job.duplicate_rows


async def test_overlapping_imports_with_rows_in_opposite_order_do_not_deadlock(
    scratch_engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    # ON CONFLICT waits on the other import's uncommitted rows, so two imports that share two
    # keys and insert them in opposite order wait on each other: PostgreSQL aborts one with a
    # deadlock and a valid upload ends as internal_error. Inserting in key order (fingerprint,
    # occurrence), whatever the file order, makes every import wait in the same order.
    shared = parse_statement_csv(HEADER + COFFEE + BOOKS)
    low, high = sorted(assign_occurrences(shared.transactions), key=lambda row: row.fingerprint)
    lines = {b"coffee shop": COFFEE, b"books": BOOKS}
    high_first = (
        lines[high.transaction.normalized_description.encode()]
        + lines[low.transaction.normalized_description.encode()]
    )

    async with sessions() as first, sessions() as second:
        job = await imports_data.insert_job(
            first,
            owner_id=OWNER_ID,
            account_id=account_id,
            file_sha256=hashlib.sha256(b"first").hexdigest(),
            outcome=completed_outcome(shared),
        )
        await imports_data.insert_transactions(first, job=job, rows=[low])
        racing = asyncio.create_task(
            import_service.import_statement(
                second,
                owner_id=OWNER_ID,
                account_id=account_id,
                content=HEADER + high_first,
                limits=LIMITS,
            )
        )
        await _wait_until_a_query_waits_on_a_lock(scratch_engine)  # the second waits on `low`

        # In file order the second import would already hold `high` uncommitted: a cycle.
        await imports_data.insert_transactions(first, job=job, rows=[high])
        await first.commit()
        result = await racing

    assert result.job.status is ImportStatus.COMPLETED
    assert result.job.duplicate_rows == 2
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Transaction)) == 2
