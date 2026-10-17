"""Statement import (ADR 0004).

1. The account must belong to the owner.
2. The file is hashed and parsed before anything is written, in a worker thread (CPU-bound work
   would block the event loop) and with no transaction open (no pooled connection is held).
3. One transaction inserts the job in its final state, then its transactions and row errors.
   Job and rows commit together, so the counters always equal the stored rows, a CSV row is
   stored either as a transaction or as an error (the parser puts it in exactly one list), and
   only completed jobs own transactions (ADR 0003 "Known consequences").
4. An unexpected database error rolls everything back and records the job as failed with
   internal_error in a second, short transaction.
5. A rejected file that was already rejected with the same error code returns that failed job
   instead of adding another one: the outcome is deterministic, and a retry loop would otherwise
   grow the table without bound. A different outcome (e.g. after a limit change) is a new job.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data import imports as imports_data
from transaction_analytics.data.errors import constraint_name, error_kind
from transaction_analytics.data.models import ACTIVE_FILE_INDEX, ImportJob, ImportRowError
from transaction_analytics.domain.csv_parser import ParseResult, parse_statement_csv
from transaction_analytics.domain.errors import FileRejectedError
from transaction_analytics.domain.imports import (
    ImportOutcome,
    completed_outcome,
    internal_error_outcome,
    rejected_outcome,
)
from transaction_analytics.services.errors import (
    AccountNotFoundError,
    DuplicateImportError,
    ImportNotFoundError,
    ImportProcessingError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_bytes: int
    max_rows: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    job: ImportJob
    # False when an identical earlier rejection was returned instead of creating a job.
    created: bool


@dataclass(frozen=True, slots=True)
class RowErrorPage:
    items: list[ImportRowError]
    # Pass as after_row to get the next page; None when this is the last page.
    next_after_row: int | None


async def import_statement(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    content: bytes,
    limits: ImportLimits,
) -> ImportResult:
    """Store the outcome of importing `content`; a rejected file yields a failed job.

    Owns the transaction boundaries: it rolls back after the account check and commits the
    import, so it must get a session with no pending work of the caller's.

    Raises AccountNotFoundError, DuplicateImportError or ImportProcessingError.
    """
    if await accounts_data.get_account(session, owner_id=owner_id, account_id=account_id) is None:
        raise AccountNotFoundError
    # End the read transaction so its connection goes back to the pool during the CPU-bound
    # parse instead of sitting "idle in transaction". The write transaction starts at the INSERT.
    await session.rollback()

    file_sha256, parsed = await asyncio.to_thread(_hash_and_parse, content, limits)
    result: ParseResult | None = None
    if isinstance(parsed, FileRejectedError):
        outcome = rejected_outcome(parsed)
    else:
        result = parsed
        outcome = completed_outcome(result)

    try:
        if result is None and outcome.error_code is not None:
            previous = await imports_data.find_failed_job(
                session,
                owner_id=owner_id,
                account_id=account_id,
                file_sha256=file_sha256,
                error_code=outcome.error_code,
            )
            if previous is not None:
                return ImportResult(job=previous, created=False)
        job = await _insert_job(session, owner_id, account_id, file_sha256, outcome)
        if result is not None:
            await imports_data.insert_transactions(
                session, job=job, transactions=result.transactions
            )
            await imports_data.insert_row_errors(session, job=job, errors=result.errors)
        await session.commit()
    except DBAPIError as exc:
        # Only the class name and the constraint name are safe to log (ADR 0003, item 10).
        logger.error(
            "Storing an import failed: %s (constraint: %s)", error_kind(exc), constraint_name(exc)
        )
        await session.rollback()
        failed_id = await _record_internal_error(session, owner_id, account_id, file_sha256)
        raise ImportProcessingError(failed_id) from None
    return ImportResult(job=job, created=True)


def _hash_and_parse(
    content: bytes, limits: ImportLimits
) -> tuple[str, ParseResult | FileRejectedError]:
    """CPU-bound work; runs in a worker thread so the event loop keeps serving other requests."""
    file_sha256 = hashlib.sha256(content).hexdigest()
    try:
        result = parse_statement_csv(content, max_bytes=limits.max_bytes, max_rows=limits.max_rows)
    except FileRejectedError as exc:
        return file_sha256, exc
    return file_sha256, result


async def _insert_job(
    session: AsyncSession,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    file_sha256: str,
    outcome: ImportOutcome,
) -> ImportJob:
    # No SELECT before the INSERT: the unique index decides, which also covers two concurrent
    # uploads of the same file. The savepoint keeps the transaction usable after a violation.
    try:
        async with session.begin_nested():
            return await imports_data.insert_job(
                session,
                owner_id=owner_id,
                account_id=account_id,
                file_sha256=file_sha256,
                outcome=outcome,
            )
    except IntegrityError as exc:
        if constraint_name(exc) != ACTIVE_FILE_INDEX:
            raise
    existing_id = await imports_data.find_active_job_id(
        session, owner_id=owner_id, account_id=account_id, file_sha256=file_sha256
    )
    if existing_id is None:
        # Unreachable while jobs are never deleted or set to failed after the fact.
        logger.error("Active-file conflict without a visible active job")
        raise ImportProcessingError(None)
    raise DuplicateImportError(existing_id)


async def _record_internal_error(
    session: AsyncSession, owner_id: uuid.UUID, account_id: uuid.UUID, file_sha256: str
) -> uuid.UUID | None:
    """Best effort: the import already failed; failing to record that must not hide it."""
    try:
        job = await imports_data.insert_job(
            session,
            owner_id=owner_id,
            account_id=account_id,
            file_sha256=file_sha256,
            outcome=internal_error_outcome(),
        )
        await session.commit()
    except DBAPIError as exc:
        logger.error("Recording a failed import failed too: %s", error_kind(exc))
        await session.rollback()
        return None
    return job.id


async def get_import(
    session: AsyncSession, *, owner_id: uuid.UUID, import_id: uuid.UUID
) -> ImportJob:
    job = await imports_data.get_job(session, owner_id=owner_id, import_job_id=import_id)
    if job is None:
        raise ImportNotFoundError
    return job


async def list_import_errors(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    import_id: uuid.UUID,
    after_row: int,
    limit: int,
) -> RowErrorPage:
    await get_import(session, owner_id=owner_id, import_id=import_id)
    # One extra row tells whether another page exists without a COUNT query.
    rows = await imports_data.list_row_errors(
        session, owner_id=owner_id, import_job_id=import_id, after_row=after_row, limit=limit + 1
    )
    if len(rows) > limit:
        return RowErrorPage(items=rows[:limit], next_after_row=rows[limit - 1].row_number)
    return RowErrorPage(items=rows, next_after_row=None)
