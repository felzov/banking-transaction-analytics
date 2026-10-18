"""Re-importing rows an account already holds (ADR 0006), through the import service.

Each import adds max(0, n - m) rows per key, where n is how often the key occurs in the file and m
how often the account already holds it. HTTP-level checks are in test_imports_api.py.
"""

import uuid
from collections import Counter
from pathlib import Path

import pytest
from factories import OTHER_OWNER_ID, OWNER_ID
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data import imports as imports_data
from transaction_analytics.data.models import ImportJob, ImportRowError, Transaction
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus
from transaction_analytics.services import imports as import_service
from transaction_analytics.services.errors import ImportProcessingError
from transaction_analytics.services.imports import ImportLimits

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"
VALID = (FIXTURES / "statement_valid.csv").read_bytes()
MIXED = (FIXTURES / "statement_mixed_errors.csv").read_bytes()
HEADER = b"date,description,amount,currency\n"
LIMITS = ImportLimits(max_bytes=5 * 1024 * 1024, max_rows=50_000)

RENT = b"2026-09-01,Rent,-950.00,GBP\n"
SALARY = b"2026-09-02,Salary,2500.00,GBP\n"
COFFEE = b"2026-09-03,Coffee Shop,-3.20,GBP\n"
BOOKS = b"2026-09-04,Books,-12.00,GBP\n"


async def new_account(session: AsyncSession, owner_id: uuid.UUID = OWNER_ID) -> uuid.UUID:
    account = await accounts_data.add_account(session, owner_id=owner_id, name="Current")
    # The service rolls back after its account check, so the account must be committed first.
    await session.commit()
    return account.id


async def run_import(
    session: AsyncSession, account_id: uuid.UUID, content: bytes, owner_id: uuid.UUID = OWNER_ID
) -> ImportJob:
    result = await import_service.import_statement(
        session, owner_id=owner_id, account_id=account_id, content=content, limits=LIMITS
    )
    assert result.created
    return result.job


async def stored_rows(session: AsyncSession, account_id: uuid.UUID) -> Counter[tuple]:
    """Every transaction of the account by its key, with how many times it is stored."""
    rows = await session.execute(
        select(
            Transaction.booked_on,
            Transaction.amount_minor,
            Transaction.currency,
            Transaction.normalized_description,
        ).where(Transaction.account_id == account_id)
    )
    return Counter(tuple(row) for row in rows)


async def job_rows(session: AsyncSession, job: ImportJob) -> list[int]:
    statement = (
        select(Transaction.row_number)
        .where(Transaction.import_job_id == job.id)
        .order_by(Transaction.row_number)
    )
    return list(await session.scalars(statement))


# --- the cases dedup exists for ------------------------------------------------------------------


async def test_overlapping_statement_adds_only_the_new_rows(session: AsyncSession) -> None:
    account_id = await new_account(session)
    await run_import(session, account_id, HEADER + RENT + SALARY + COFFEE)

    job = await run_import(session, account_id, HEADER + SALARY + COFFEE + BOOKS)

    assert job.status is ImportStatus.COMPLETED
    assert (job.total_rows, job.valid_rows, job.duplicate_rows) == (3, 3, 2)
    assert await job_rows(session, job) == [4]  # Books, the file's third data row
    assert sum((await stored_rows(session, account_id)).values()) == 4


async def test_corrected_file_adds_only_the_fixed_rows(session: AsyncSession) -> None:
    # The scenario from README "Known limitations" before Phase 4: re-uploading a fixed file
    # used to store its previously valid rows (Rent, Refund) a second time.
    account_id = await new_account(session)
    first = await run_import(session, account_id, MIXED)
    # Read now: the next import's rollback expires every object loaded in this session.
    first_counts = (first.valid_rows, first.duplicate_rows)
    corrected = (
        MIXED.replace(b"2026-09-31,Bad date", b"2026-09-30,Bad date")
        .replace(b"-1.005", b"-1.01")
        .replace(b'"1,200.00"', b"1200.00")
    )

    second = await run_import(session, account_id, corrected)

    assert first_counts == (2, 0)
    assert (second.valid_rows, second.invalid_rows, second.duplicate_rows) == (5, 3, 2)
    assert await job_rows(session, second) == [3, 4, 5]
    assert set((await stored_rows(session, account_id)).values()) == {1}
    # Row errors of the corrected file are still reported.
    errors = await session.scalar(
        select(func.count()).select_from(ImportRowError).filter_by(import_job_id=second.id)
    )
    assert errors == 3


async def test_same_statement_with_a_bom_adds_nothing(session: AsyncSession) -> None:
    # Different bytes, so not the file-level 409, but every row is already there.
    account_id = await new_account(session)
    await run_import(session, account_id, VALID)

    job = await run_import(session, account_id, b"\xef\xbb\xbf" + VALID)

    assert job.status is ImportStatus.COMPLETED
    assert (job.valid_rows, job.duplicate_rows) == (5, 5)
    assert await job_rows(session, job) == []
    assert sum((await stored_rows(session, account_id)).values()) == 5


# --- multiplicity: identical rows are different transactions ------------------------------------


async def test_identical_rows_in_one_file_are_all_kept(session: AsyncSession) -> None:
    account_id = await new_account(session)

    job = await run_import(session, account_id, HEADER + COFFEE * 3)

    assert job.duplicate_rows == 0
    assert list((await stored_rows(session, account_id)).values()) == [3]


async def test_a_file_with_more_copies_adds_the_difference(session: AsyncSession) -> None:
    account_id = await new_account(session)
    await run_import(session, account_id, HEADER + COFFEE * 2)

    job = await run_import(session, account_id, HEADER + COFFEE * 3 + RENT)

    assert job.duplicate_rows == 2
    # The third copy (row 4) is the new one; occurrences are numbered in file order.
    assert await job_rows(session, job) == [4, 5]
    assert list((await stored_rows(session, account_id)).values()).count(3) == 1


async def test_a_file_with_fewer_copies_adds_nothing(session: AsyncSession) -> None:
    account_id = await new_account(session)
    await run_import(session, account_id, HEADER + COFFEE * 2)

    job = await run_import(session, account_id, HEADER + COFFEE + RENT)

    assert job.duplicate_rows == 1
    assert await job_rows(session, job) == [3]
    stored = await stored_rows(session, account_id)
    assert sorted(stored.values()) == [1, 2]


# --- what is not a duplicate --------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        b"2026-09-04,Coffee Shop,-3.20,GBP\n",  # date
        b"2026-09-03,Coffee Shop,-3.21,GBP\n",  # amount
        b"2026-09-03,Coffee Shop,3.20,GBP\n",  # direction
        b"2026-09-03,Coffee Shop,-3.20,EUR\n",  # currency
        b"2026-09-03,Coffee Shop Ltd,-3.20,GBP\n",  # description
    ],
    ids=["date", "amount", "sign", "currency", "description"],
)
async def test_a_row_differing_in_one_field_is_new(session: AsyncSession, variant: bytes) -> None:
    account_id = await new_account(session)
    await run_import(session, account_id, HEADER + COFFEE)

    job = await run_import(session, account_id, HEADER + variant)

    assert job.duplicate_rows == 0
    assert sum((await stored_rows(session, account_id)).values()) == 2


async def test_spelling_variants_are_duplicates(session: AsyncSession) -> None:
    account_id = await new_account(session)
    await run_import(session, account_id, HEADER + COFFEE)

    job = await run_import(session, account_id, HEADER + b"2026-09-03,  COFFEE   shop ,-3.2,gbp\n")

    assert job.duplicate_rows == 1


async def test_dedup_is_per_account(session: AsyncSession) -> None:
    current, savings = await new_account(session), await new_account(session)
    await run_import(session, current, HEADER + COFFEE)

    job = await run_import(session, savings, HEADER + COFFEE + RENT)

    assert job.duplicate_rows == 0


async def test_dedup_is_per_owner(session: AsyncSession) -> None:
    mine, theirs = await new_account(session), await new_account(session, OTHER_OWNER_ID)
    await run_import(session, mine, HEADER + COFFEE)

    job = await run_import(session, theirs, HEADER + COFFEE + RENT, owner_id=OTHER_OWNER_ID)

    assert job.duplicate_rows == 0


# --- invariants and failure paths ---------------------------------------------------------------


async def test_stored_rows_always_equal_valid_minus_duplicate_rows(session: AsyncSession) -> None:
    account_id = await new_account(session)
    files = [MIXED, VALID, HEADER + COFFEE * 3 + RENT, b"\xef\xbb\xbf" + VALID, HEADER + COFFEE]

    returned = []
    for content in files:
        job = await run_import(session, account_id, content)
        # Read now: the next import's rollback expires every object loaded in this session.
        returned.append((job.id, job.valid_rows, job.duplicate_rows))

    for job_id, valid_rows, duplicate_rows in returned:
        stored = await session.scalar(
            select(func.count()).select_from(Transaction).filter_by(import_job_id=job_id)
        )
        assert stored == valid_rows - duplicate_rows
        # What the service returned is what was committed.
        committed = await session.scalar(
            select(ImportJob.duplicate_rows).where(ImportJob.id == job_id)
        )
        assert committed == duplicate_rows
    assert [duplicate_rows for *_, duplicate_rows in returned] == [0, 0, 1, 5, 1]


async def test_rejected_file_has_no_duplicates(session: AsyncSession) -> None:
    job = await run_import(session, await new_account(session), HEADER)

    assert job.status is ImportStatus.FAILED
    assert job.duplicate_rows == 0


async def test_failure_after_the_insert_rolls_back_every_row(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rows are inserted before duplicate_rows is set; if that update fails, nothing may stay.
    account_id = await new_account(session)
    await run_import(session, account_id, HEADER + COFFEE)

    async def failing_update(session: AsyncSession, **values: object) -> None:
        raise DBAPIError("UPDATE import_jobs ...", None, Exception("boom"))

    monkeypatch.setattr(imports_data, "set_duplicate_rows", failing_update)

    with pytest.raises(ImportProcessingError) as exc:
        await import_service.import_statement(
            session,
            owner_id=OWNER_ID,
            account_id=account_id,
            content=HEADER + COFFEE + RENT,
            limits=LIMITS,
        )

    failed = await session.get(ImportJob, exc.value.failed_import_id)
    assert failed is not None
    assert failed.error_code is ImportFailureCode.INTERNAL_ERROR
    assert sum((await stored_rows(session, account_id)).values()) == 1  # Rent was rolled back
