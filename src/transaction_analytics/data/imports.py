"""Import job, transaction and row error persistence.

Every function requires owner_id; there is no unscoped lookup. Composite foreign keys (ADR 0003)
additionally stop a write from attaching rows to another owner's account or job.
"""

import uuid
from collections.abc import Iterable, Sequence
from itertools import batched
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import ImportJob, ImportRowError, Transaction
from transaction_analytics.domain.csv_parser import ParsedTransaction, RowError
from transaction_analytics.domain.imports import ImportFailureCode, ImportOutcome, ImportStatus

# Rows per INSERT call. One call for 50 000 rows made SQLAlchemy prepare every parameter set in a
# single step and stalled the event loop for ~0.4 s; between bounded calls other requests run.
INSERT_BATCH_SIZE = 1_000


async def insert_job(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    file_sha256: str,
    outcome: ImportOutcome,
) -> ImportJob:
    """Insert a job directly in its final state (ADR 0004)."""
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
    session: AsyncSession, model: type, rows: Iterable[dict[str, Any]]
) -> None:
    # Each call gets a list of parameter sets, which SQLAlchemy sends as multi-row INSERTs.
    for batch in batched(rows, INSERT_BATCH_SIZE):
        await session.execute(insert(model), list(batch))


async def insert_transactions(
    session: AsyncSession, *, job: ImportJob, transactions: Sequence[ParsedTransaction]
) -> None:
    await _insert_in_batches(
        session,
        Transaction,
        (
            {
                "owner_id": job.owner_id,
                "account_id": job.account_id,
                "import_job_id": job.id,
                "row_number": parsed.row_number,
                "booked_on": parsed.booked_on,
                "description": parsed.description,
                "normalized_description": parsed.normalized_description,
                "amount_minor": parsed.amount_minor,
                "currency": parsed.currency,
            }
            for parsed in transactions
        ),
    )


async def insert_row_errors(
    session: AsyncSession, *, job: ImportJob, errors: Sequence[RowError]
) -> None:
    await _insert_in_batches(
        session,
        ImportRowError,
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
