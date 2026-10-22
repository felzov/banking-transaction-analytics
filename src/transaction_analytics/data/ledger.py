"""Ledger sync and ledger transaction persistence (ADR 0009).

Every function requires owner_id (and the account, where rows belong to one); there is no unscoped
lookup. The composite foreign keys additionally stop a row from attaching to another owner's sync
or to an account of the other source.
"""

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.imports import insert_in_batches
from transaction_analytics.data.models import DEDUP_CONSTRAINT, LedgerSync, Transaction
from transaction_analytics.domain.ledger import AccountSource, LedgerTransaction


async def insert_sync(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID, fetched_entries: int
) -> LedgerSync:
    """Insert a sync in its final state except duplicate_entries (set_duplicate_entries)."""
    statement = (
        insert(LedgerSync)
        .values(owner_id=owner_id, account_id=account_id, fetched_entries=fetched_entries)
        .returning(LedgerSync)
    )
    return (await session.scalars(statement)).one()


async def known_fingerprints(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    fingerprints: Iterable[bytes],
) -> set[bytes]:
    """Which of these fingerprints the account already holds; served by uq_transactions_dedup."""
    wanted = list(fingerprints)
    if not wanted:
        return set()
    statement = select(Transaction.fingerprint).where(
        Transaction.owner_id == owner_id,
        Transaction.account_id == account_id,
        Transaction.fingerprint.in_(wanted),
    )
    return set(await session.scalars(statement))


def in_insert_order(rows: Iterable[LedgerTransaction]) -> list[LedgerTransaction]:
    """By fingerprint, like every writer of transactions (ADR 0006 item 4): a ledger row's
    occurrence is always 1, so this is the (fingerprint, occurrence) order imports use."""
    return sorted(rows, key=lambda row: row.fingerprint)


async def insert_ledger_transactions(
    session: AsyncSession, *, sync: LedgerSync, rows: Sequence[LedgerTransaction]
) -> int:
    """Insert the entries the account does not hold yet; returns how many were inserted.

    Like insert_transactions for imports: the database skips an entry the account already has
    (ON CONFLICT on uq_transactions_dedup, no prior SELECT), and rows go in key order so two
    concurrent syncs of one account wait on each other in the same order and cannot deadlock.
    """
    statement = (
        postgresql.insert(Transaction)
        .on_conflict_do_nothing(constraint=DEDUP_CONSTRAINT)
        # RETURNING yields only the rows that were inserted, not the skipped ones.
        .returning(Transaction.id)
    )
    parameters = (
        {
            "owner_id": sync.owner_id,
            "account_id": sync.account_id,
            "source": AccountSource.LEDGER,
            "ledger_sync_id": sync.id,
            "ledger_entry_id": row.entry_id,
            "booked_on": row.booked_on,
            "description": row.description,
            "normalized_description": row.normalized_description,
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "fingerprint": row.fingerprint,
            "occurrence": 1,
        }
        for row in in_insert_order(rows)
    )
    return await insert_in_batches(session, statement, parameters)


async def set_duplicate_entries(
    session: AsyncSession, *, sync: LedgerSync, duplicate_entries: int
) -> None:
    """Known only after the entries are inserted; runs in the transaction that inserted the sync."""
    await session.execute(
        update(LedgerSync)
        .where(LedgerSync.owner_id == sync.owner_id, LedgerSync.id == sync.id)
        .values(duplicate_entries=duplicate_entries)
    )


async def get_sync(
    session: AsyncSession, *, owner_id: uuid.UUID, sync_id: uuid.UUID
) -> LedgerSync | None:
    statement = select(LedgerSync).where(LedgerSync.owner_id == owner_id, LedgerSync.id == sync_id)
    return await session.scalar(statement)
