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
from transaction_analytics.domain.csv_parser import ParsedTransaction, ParseResult
from transaction_analytics.domain.imports import (
    ImportFailureCode,
    ImportOutcome,
    ImportStatus,
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
