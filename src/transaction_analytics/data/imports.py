"""Import job, transaction and row error persistence.

Every function requires owner_id; there is no unscoped lookup. Composite foreign keys (ADR 0003)
additionally stop a write from attaching rows to another owner's account or job.
"""

import uuid
from collections.abc import Iterable, Sequence
from itertools import batched
from typing import Any

from sqlalchemy import Insert, func, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import (
    DEDUP_CONSTRAINT,
    ImportJob,
    ImportRowError,
    Transaction,
)
from transaction_analytics.domain.csv_parser import RowError
from transaction_analytics.domain.dedup import DedupRow
from transaction_analytics.domain.imports import ImportFailureCode, ImportOutcome, ImportStatus

# Rows per INSERT call. One call for 50 000 rows made SQLAlchemy prepare every parameter set in a
# single step and stalled the event loop for ~0.4 s; between bounded calls other requests run.
# 1 000 was enough until rows were inserted in key order (ADR 0006): rows of one batch are then
# scattered in memory and the longest stall rose from ~0.03 s to ~0.08 s. 250 brings it back to
# ~0.03 s with no change in total time (50 000 rows, measured).
INSERT_BATCH_SIZE = 250


async def insert_job(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    file_sha256: str,
    outcome: ImportOutcome,
) -> ImportJob:
    """Insert a job in its final state (ADR 0004), except duplicate_rows, which is set after the
    transactions are inserted (set_duplicate_rows, ADR 0006)."""
    statement = (
        insert(ImportJob)
        .values(
            owner_id=owner_id,
            account_id=account_id,
            file_sha256=file_sha256,
            status=outcome.status,
            total_rows=outcome.total_rows,
            valid_rows=outcome.valid_rows,
            invalid_rows=outcome.invalid_rows,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            # Database clock (ADR 0003). The import is one transaction, so both are its start.
            started_at=func.now(),
            finished_at=func.now(),
        )
        .returning(ImportJob)
    )
    return (await session.scalars(statement)).one()


async def find_active_job_id(
    session: AsyncSession, *, owner_id: uuid.UUID, account_id: uuid.UUID, file_sha256: str
) -> uuid.UUID | None:
    """The job that holds the active-file unique index for this file, if any."""
    statement = select(ImportJob.id).where(
        ImportJob.owner_id == owner_id,
        ImportJob.account_id == account_id,
        ImportJob.file_sha256 == file_sha256,
        ImportJob.status != ImportStatus.FAILED,
    )
    return await session.scalar(statement)


async def _insert_in_batches(
    session: AsyncSession, statement: Insert, rows: Iterable[dict[str, Any]]
) -> int:
    """Run `statement` once per batch; returns how many rows its RETURNING produced, if any."""
    has_returning = bool(statement.exported_columns)  # the RETURNING columns; empty without it
    returned = 0
    # Each call gets a list of parameter sets, which SQLAlchemy sends as multi-row INSERTs.
    for batch in batched(rows, INSERT_BATCH_SIZE):
        result = await session.execute(statement, list(batch))
        if has_returning:
            returned += len(result.all())
    return returned


def _key_order(row: DedupRow) -> bytes:
    # Orders like (fingerprint, occurrence): the fingerprint is always 32 bytes and big-endian
    # bytes order like the number. Flat bytes compare with memcmp; sorting tuples took twice as
    # long, and that comparison phase holds the GIL, so it stalls the event loop even when it runs
    # in a worker thread. An occurrence never exceeds the row limit, far below 2**32.
    return row.fingerprint + row.occurrence.to_bytes(4, "big")


def in_insert_order(rows: Iterable[DedupRow]) -> list[DedupRow]:
    """Rows in the order insert_transactions writes them (see there).

    The import service calls this in its worker thread; insert_transactions sorts again to
    guarantee the order, which on sorted rows is one linear pass.
    """
    return sorted(rows, key=_key_order)


async def insert_transactions(
    session: AsyncSession, *, job: ImportJob, rows: Sequence[DedupRow]
) -> int:
    """Insert the rows the account does not hold yet (ADR 0006); returns how many were inserted.

    A row whose (fingerprint, occurrence) the account already has is skipped by the database, not
    by a prior SELECT, so two concurrent imports cannot both insert it: the second waits for the
    first to commit, then skips. Any other violation still raises.

    That wait is a lock: rows are inserted in key order, never file order, so every import waits
    in the same order and two imports of overlapping files cannot deadlock. Which copy of a key is
    new does not change: occurrences were already numbered in file order.
    """
    statement = (
        postgresql.insert(Transaction)
        .on_conflict_do_nothing(constraint=DEDUP_CONSTRAINT)
        # RETURNING yields only the rows that were inserted, not the skipped ones.
        .returning(Transaction.row_number)
    )
    parameters = (
        {
            "owner_id": job.owner_id,
            "account_id": job.account_id,
            "import_job_id": job.id,
            "row_number": row.transaction.row_number,
            "booked_on": row.transaction.booked_on,
            "description": row.transaction.description,
            "normalized_description": row.transaction.normalized_description,
            "amount_minor": row.transaction.amount_minor,
            "currency": row.transaction.currency,
            "fingerprint": row.fingerprint,
            "occurrence": row.occurrence,
        }
        for row in in_insert_order(rows)
    )
    return await _insert_in_batches(session, statement, parameters)


async def set_duplicate_rows(session: AsyncSession, *, job: ImportJob, duplicate_rows: int) -> None:
    """Known only after the rows are inserted; runs in the transaction that inserted the job."""
    await session.execute(
        update(ImportJob)
        .where(ImportJob.owner_id == job.owner_id, ImportJob.id == job.id)
        .values(duplicate_rows=duplicate_rows)
    )


async def insert_row_errors(
    session: AsyncSession, *, job: ImportJob, errors: Sequence[RowError]
) -> None:
    await _insert_in_batches(
        session,
        insert(ImportRowError),
        (
            {
                "owner_id": job.owner_id,
                "import_job_id": job.id,
                "row_number": error.row_number,
                "code": error.code,
                "column_name": error.column,
                "message": error.message,
            }
            for error in errors
        ),
    )


async def find_failed_job(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    file_sha256: str,
    error_code: ImportFailureCode,
) -> ImportJob | None:
    """The latest failed import of this file into this account with this error code."""
    statement = (
        select(ImportJob)
        .where(
            ImportJob.owner_id == owner_id,
            ImportJob.account_id == account_id,
            ImportJob.file_sha256 == file_sha256,
            ImportJob.status == ImportStatus.FAILED,
            ImportJob.error_code == error_code,
        )
        .order_by(ImportJob.created_at.desc())
        .limit(1)
    )
    return await session.scalar(statement)


async def get_job(
    session: AsyncSession, *, owner_id: uuid.UUID, import_job_id: uuid.UUID
) -> ImportJob | None:
    statement = select(ImportJob).where(
        ImportJob.owner_id == owner_id, ImportJob.id == import_job_id
    )
    return await session.scalar(statement)


async def list_row_errors(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    import_job_id: uuid.UUID,
    after_row: int,
    limit: int,
) -> list[ImportRowError]:
    """Keyset page ordered by row_number; served by UNIQUE (import_job_id, row_number)."""
    statement = (
        select(ImportRowError)
        .where(
            ImportRowError.owner_id == owner_id,
            ImportRowError.import_job_id == import_job_id,
            ImportRowError.row_number > after_row,
        )
        .order_by(ImportRowError.row_number)
        .limit(limit)
    )
    return list(await session.scalars(statement))
