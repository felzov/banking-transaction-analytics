"""Ledger syncs that need real COMMITs and separate sessions (concurrency).

Runs on its own throwaway database (committed_database_url), like test_committed_imports.py.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from factories import OWNER_ID
from fake_ledger import TOKEN, FakeLedger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data.db import create_engine, create_sessionmaker
from transaction_analytics.data.models import LedgerSync, Transaction
from transaction_analytics.domain.ledger import LedgerLink
from transaction_analytics.integrations.ledger import LedgerConfig
from transaction_analytics.services import ledger_sync as ledger_sync_service
from transaction_analytics.services.ledger_sync import LedgerSettings

pytestmark = pytest.mark.integration


@pytest.fixture
async def scratch_engine(committed_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(committed_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(scratch_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(scratch_engine)


async def test_concurrent_syncs_store_each_entry_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # Both syncs read the statement before either writes (the fake ledger answers slowly), so both
    # try to insert every entry. The second waits on the first's uncommitted rows, then skips them:
    # no duplicates, no deadlock (rows go in key order, ADR 0006), and both syncs succeed. The one
    # that found nothing new after all records nothing.
    ledger = FakeLedger(max_page_size=7, delay=0.05)
    for index in range(40):
        ledger.post("deposit", 100 + index)
    settings = LedgerSettings(
        client=LedgerConfig(base_url="http://ledger.test", timeout_seconds=5.0),
        sync_timeout_seconds=30.0,
        max_entries=1000,
        transport=ledger.transport(),
    )
    async with sessions() as session:
        account = await accounts_data.add_account(
            session,
            owner_id=OWNER_ID,
            name="Ledger",
            ledger=LedgerLink(user_id=ledger.user_id, account_id=ledger.account_id),
        )
        await session.commit()

    async def run_sync() -> ledger_sync_service.LedgerSyncResult:
        async with sessions() as session:
            return await ledger_sync_service.sync_account(
                session, owner_id=OWNER_ID, account_id=account.id, token=TOKEN, ledger=settings
            )

    first, second = await asyncio.gather(run_sync(), run_sync())

    assert first.fetched_entries == second.fetched_entries == 40  # both read everything
    assert first.duplicate_entries + second.duplicate_entries == 40  # each entry stored once
    assert [result.sync is not None for result in (first, second)].count(True) == 1
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.account_id == account.id)
        )
        distinct = await session.scalar(
            select(func.count(func.distinct(Transaction.ledger_entry_id))).where(
                Transaction.account_id == account.id
            )
        )
        syncs = await session.scalar(
            select(func.count()).select_from(LedgerSync).where(LedgerSync.account_id == account.id)
        )
    assert count == distinct == 40
    assert syncs == 1
