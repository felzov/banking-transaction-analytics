"""Every schema rule, tested against the migrated database.

Each violation case breaks exactly one rule and asserts *which* constraint fired, so a test cannot
pass because a different rule happened to reject the row.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import pytest
from factories import (
    FINISHED,
    OTHER_OWNER_ID,
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
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.errors import constraint_name
from transaction_analytics.data.models import Account, Base, ImportJob, Transaction
from transaction_analytics.domain.csv_parser import REQUIRED_COLUMNS
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus
from transaction_analytics.domain.money import Currency

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class World:
    """Persisted, valid rows that violation cases point at."""

    acc: Account
    acc2: Account  # second account of the same owner
    other_acc: Account  # another owner's account
    job: ImportJob  # completed job on acc, with a transaction at row 2 and an error at row 3
    tx: Transaction  # the transaction at row 2


@pytest.fixture
async def world(session: AsyncSession) -> World:
    acc, acc2, other_acc = account(), account(name="Savings"), account(OTHER_OWNER_ID)
    session.add_all([acc, acc2, other_acc])
    await session.flush()
    import_job = completed(acc, file_sha256=sha256_hex(1))
    session.add(import_job)
    await session.flush()
    tx = transaction(import_job, row_number=2)
    session.add_all([tx, row_error(import_job, row_number=3)])
    await session.flush()
    return World(acc, acc2, other_acc, import_job, tx)


Build = Callable[[World], list[Base]]

VIOLATIONS: list[tuple[str, Build, str]] = [
    # accounts
    ("account: blank name", lambda w: [account(name="   ")], "ck_accounts_name_valid"),
    ("account: name > 100", lambda w: [account(name="x" * 101)], "ck_accounts_name_valid"),
    ("account: empty name", lambda w: [account(name="")], "ck_accounts_name_valid"),
    ("account: leading space", lambda w: [account(name=" Current")], "ck_accounts_name_valid"),
    ("account: trailing space", lambda w: [account(name="Current ")], "ck_accounts_name_valid"),
    ("account: tabs only", lambda w: [account(name="\t\t")], "ck_accounts_name_valid"),
    ("account: inner newline", lambda w: [account(name="Cur\nrent")], "ck_accounts_name_valid"),
    ("account: control character", lambda w: [account(name="Cur\x07")], "ck_accounts_name_valid"),
    # import_jobs: values
    (
        "job: unknown status",
        lambda w: [job(w.acc, status="done", started_at=STARTED)],
        "ck_import_jobs_status_allowed",
    ),
    (
        "job: uppercase hash",
        lambda w: [job(w.acc, file_sha256="A" * 64)],
        "ck_import_jobs_file_sha256_hex",
    ),
    (
        "job: short hash",
        lambda w: [job(w.acc, file_sha256="a" * 63)],
        "ck_import_jobs_file_sha256_hex",
    ),
    (
        "job: non-hex hash",
        lambda w: [job(w.acc, file_sha256="g" * 64)],
        "ck_import_jobs_file_sha256_hex",
    ),
    (
        "job: negative count",
        # On invalid_rows: a negative valid_rows also breaks duplicate_rows <= valid_rows, so it
        # cannot isolate this rule (see test_negative_valid_rows_is_rejected).
        lambda w: [job(w.acc, total_rows=0, valid_rows=1, invalid_rows=-1)],
        "ck_import_jobs_row_counts_non_negative",
    ),
    (
        "job: counts do not add up",
        lambda w: [job(w.acc, total_rows=3, valid_rows=1, invalid_rows=1)],
        "ck_import_jobs_row_counts_add_up",
    ),
    (
        "job: negative duplicate count",
        lambda w: [completed(w.acc, duplicate_rows=-1)],
        "ck_import_jobs_duplicate_rows_valid",
    ),
    (
        "job: more duplicates than valid rows",
        lambda w: [completed(w.acc, valid_rows=2, duplicate_rows=3)],
        "ck_import_jobs_duplicate_rows_valid",
    ),
    # import_jobs: failure details
    (
        "job: unknown error code",
        lambda w: [failed(w.acc, error_code="too_many_invalid_rows")],
        "ck_import_jobs_error_code_allowed",
    ),
    (
        "job: failed without error code",
        lambda w: [failed(w.acc, error_code=None, error_message=None)],
        "ck_import_jobs_error_code_iff_failed",
    ),
    (
        "job: completed with error code",
        lambda w: [
            completed(w.acc, error_code=ImportFailureCode.INTERNAL_ERROR, error_message="x")
        ],
        "ck_import_jobs_error_code_iff_failed",
    ),
    (
        "job: error code without message",
        lambda w: [failed(w.acc, error_message=None)],
        "ck_import_jobs_error_message_iff_code",
    ),
    (
        "job: empty error message",
        lambda w: [failed(w.acc, error_message="")],
        "ck_import_jobs_error_message_length",
    ),
    (
        "job: error message > 500",
        lambda w: [failed(w.acc, error_message="x" * 501)],
        "ck_import_jobs_error_message_length",
    ),
    # import_jobs: lifecycle timestamps
    (
        "job: pending but started",
        lambda w: [job(w.acc, started_at=STARTED)],
        "ck_import_jobs_started_at_iff_not_pending",
    ),
    (
        "job: processing but not started",
        lambda w: [job(w.acc, status=ImportStatus.PROCESSING)],
        "ck_import_jobs_started_at_iff_not_pending",
    ),
    (
        "job: processing but finished",
        lambda w: [
            job(w.acc, status=ImportStatus.PROCESSING, started_at=STARTED, finished_at=FINISHED)
        ],
        "ck_import_jobs_finished_at_iff_finished",
    ),
    (
        "job: completed but not finished",
        lambda w: [completed(w.acc, finished_at=None)],
        "ck_import_jobs_finished_at_iff_finished",
    ),
    (
        "job: finished before started",
        lambda w: [completed(w.acc, started_at=FINISHED, finished_at=STARTED)],
        "ck_import_jobs_finished_after_started",
    ),
    # import_jobs: ownership and uniqueness
    (
        "job: another owner's account",
        lambda w: [job(w.other_acc, owner_id=OWNER_ID)],
        "fk_import_jobs_account",
    ),
    (
        "job: unknown account",
        lambda w: [job(w.acc, account_id=uuid.uuid4())],
        "fk_import_jobs_account",
    ),
    (
        "job: same file again for the same account",
        lambda w: [job(w.acc, file_sha256=w.job.file_sha256)],
        "uq_import_jobs_active_file_sha256",
    ),
    # transactions: values
    (
        "transaction: row 1 is the header",
        lambda w: [transaction(w.job, row_number=1)],
        "ck_transactions_row_number_after_header",
    ),
    (
        "transaction: empty description",
        lambda w: [transaction(w.job, row_number=9, description="")],
        "ck_transactions_description_length",
    ),
    (
        "transaction: description > 500",
        lambda w: [transaction(w.job, row_number=9, description="x" * 501)],
        "ck_transactions_description_length",
    ),
    (
        "transaction: empty normalized description",
        lambda w: [transaction(w.job, row_number=9, normalized_description="")],
        "ck_transactions_normalized_description_length",
    ),
    (
        "transaction: normalized description > 1500",
        lambda w: [transaction(w.job, row_number=9, normalized_description="x" * 1501)],
        "ck_transactions_normalized_description_length",
    ),
    (
        "transaction: unknown currency",
        lambda w: [transaction(w.job, row_number=9, currency="XYZ")],
        "ck_transactions_currency_allowed",
    ),
    (
        "transaction: lowercase currency",
        lambda w: [transaction(w.job, row_number=9, currency="gbp")],
        "ck_transactions_currency_allowed",
    ),
    (
        "transaction: fingerprint is not 32 bytes",
        lambda w: [transaction(w.job, row_number=9, fingerprint=b"x" * 31)],
        "ck_transactions_fingerprint_length",
    ),
    (
        "transaction: occurrence 0",
        lambda w: [transaction(w.job, row_number=9, occurrence=0)],
        "ck_transactions_occurrence_positive",
    ),
    # transactions: ownership and uniqueness
    (
        "transaction: same key and occurrence again in the account",
        lambda w: [transaction(w.job, row_number=9, fingerprint=w.tx.fingerprint)],
        "uq_transactions_dedup",
    ),
    (
        "transaction: account differs from its job's account",
        lambda w: [transaction(w.job, row_number=9, account_id=w.acc2.id)],
        "fk_transactions_import_job",
    ),
    (
        "transaction: another owner's job",
        lambda w: [
            transaction(w.job, row_number=9, owner_id=OTHER_OWNER_ID, account_id=w.other_acc.id)
        ],
        "fk_transactions_import_job",
    ),
    (
        "transaction: duplicate row in one job",
        lambda w: [transaction(w.job, row_number=2)],
        "uq_transactions_import_job_id_row_number",
    ),
    # import_row_errors
    (
        "row error: row 1 is the header",
        lambda w: [row_error(w.job, row_number=1)],
        "ck_import_row_errors_row_number_after_header",
    ),
    (
        "row error: unknown code",
        lambda w: [row_error(w.job, row_number=9, code="boom")],
        "ck_import_row_errors_code_allowed",
    ),
    (
        "row error: column from the file header",
        lambda w: [row_error(w.job, row_number=9, column_name="reference")],
        "ck_import_row_errors_column_name_allowed",
    ),
    (
        "row error: empty message",
        lambda w: [row_error(w.job, row_number=9, message="")],
        "ck_import_row_errors_message_length",
    ),
    (
        "row error: message > 500",
        lambda w: [row_error(w.job, row_number=9, message="x" * 501)],
        "ck_import_row_errors_message_length",
    ),
    (
        "row error: another owner's job",
        lambda w: [row_error(w.job, row_number=9, owner_id=OTHER_OWNER_ID)],
        "fk_import_row_errors_import_job",
    ),
    (
        "row error: duplicate row in one job",
        lambda w: [row_error(w.job, row_number=3)],
        "uq_import_row_errors_import_job_id_row_number",
    ),
]


@pytest.mark.parametrize(
    ("build", "expected"),
    [pytest.param(build, expected, id=case) for case, build, expected in VIOLATIONS],
)
async def test_violation_is_rejected_by_named_constraint(
    session: AsyncSession, world: World, build: Build, expected: str
) -> None:
    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            session.add_all(build(world))
            await session.flush()

    assert constraint_name(exc.value) == expected


async def test_same_file_is_allowed_for_another_account_and_another_owner(
    session: AsyncSession, world: World
) -> None:
    session.add_all(
        [
            completed(w_acc, file_sha256=world.job.file_sha256)
            for w_acc in (world.acc2, world.other_acc)
        ]
    )
    await session.flush()


async def test_failed_imports_do_not_block_the_same_file(
    session: AsyncSession, world: World
) -> None:
    # ADR 0004: failed attempts stay as history and the file can be uploaded again.
    sha = sha256_hex(7)
    session.add_all([failed(world.acc2, file_sha256=sha), failed(world.acc2, file_sha256=sha)])
    await session.flush()
    session.add(completed(world.acc2, file_sha256=sha))
    await session.flush()

    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            session.add(completed(world.acc2, file_sha256=sha))
            await session.flush()

    assert constraint_name(exc.value) == "uq_import_jobs_active_file_sha256"


async def test_dedup_key_is_unique_only_per_account_and_occurrence(
    session: AsyncSession, world: World
) -> None:
    # ADR 0006: the next occurrence of a key in the same account, and the same key in another
    # account or for another owner, are all different transactions.
    job2, other_job = completed(world.acc2), completed(world.other_acc)
    session.add_all([job2, other_job])
    await session.flush()
    session.add_all(
        [
            transaction(world.job, row_number=9, fingerprint=world.tx.fingerprint, occurrence=2),
            transaction(job2, fingerprint=world.tx.fingerprint),
            transaction(other_job, fingerprint=world.tx.fingerprint),
        ]
    )
    await session.flush()


async def test_account_name_the_domain_accepts_is_stored(session: AsyncSession) -> None:
    # ADR 0003 item 13: emoji with a zero-width joiner and an RTL mark are format characters (Cf),
    # kept by the domain, so the CHECK must accept them too.
    session.add(account(name="Family \U0001f468\u200d\U0001f469 \u200f\u062d\u0633\u0627\u0628"))
    await session.flush()


async def test_a_row_can_be_both_a_transaction_and_an_error(
    session: AsyncSession, world: World
) -> None:
    # Known limitation (ADR 0003): the schema cannot forbid this; the import service must.
    session.add(row_error(world.job, row_number=2))
    await session.flush()


async def test_deleting_an_account_with_imports_is_restricted(session: AsyncSession) -> None:
    acc = account()
    session.add(acc)
    await session.flush()
    session.add(job(acc))
    await session.flush()

    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            await session.execute(delete(Account).where(Account.id == acc.id))

    assert constraint_name(exc.value) == "fk_import_jobs_account"


async def test_deleting_an_import_job_with_transactions_is_restricted(
    session: AsyncSession, world: World
) -> None:
    import_job = completed(world.acc2)
    session.add(import_job)
    await session.flush()
    session.add(transaction(import_job))
    await session.flush()

    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            await session.execute(delete(ImportJob).where(ImportJob.id == import_job.id))

    assert constraint_name(exc.value) == "fk_transactions_import_job"


async def _definition(session: AsyncSession, constraint: str) -> str:
    definition = await session.scalar(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
        {"name": constraint},
    )
    assert definition is not None, constraint
    return definition


async def _literals(session: AsyncSession, constraint: str) -> set[str]:
    return set(re.findall(r"'([^']*)'::text", await _definition(session, constraint)))


async def test_negative_valid_rows_is_rejected(session: AsyncSession, world: World) -> None:
    # Breaks two rules at once: valid_rows >= 0 and duplicate_rows <= valid_rows (duplicate_rows
    # is never negative). PostgreSQL reports the first CHECK by name, so the name cannot show that
    # valid_rows >= 0 still exists; test_row_counts_check_covers_both_counts does.
    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            session.add(job(world.acc, total_rows=0, valid_rows=-1, invalid_rows=1))
            await session.flush()

    assert constraint_name(exc.value) in {
        "ck_import_jobs_row_counts_non_negative",
        "ck_import_jobs_duplicate_rows_valid",
    }


async def test_row_counts_check_covers_both_counts(session: AsyncSession) -> None:
    definition = await _definition(session, "ck_import_jobs_row_counts_non_negative")

    # Parenthesised: "invalid_rows >= 0" alone would also match "valid_rows >= 0".
    assert "(valid_rows >= 0)" in definition
    assert "(invalid_rows >= 0)" in definition


@pytest.mark.parametrize(
    ("constraint", "values"),
    [
        ("ck_transactions_currency_allowed", Currency),
        ("ck_import_jobs_error_code_allowed", ImportFailureCode),
        ("ck_import_row_errors_code_allowed", FieldErrorCode),
        ("ck_import_row_errors_column_name_allowed", REQUIRED_COLUMNS),
    ],
)
async def test_check_constraint_values_match_the_domain(
    session: AsyncSession, constraint: str, values: type[StrEnum] | tuple[str, ...]
) -> None:
    # The migration repeats the domain values as literals; this keeps the two in sync.
    assert await _literals(session, constraint) == {str(value) for value in values}


# Every CHECK on import_jobs that mentions status, with the statuses it must name.
STATUS_CHECKS: dict[str, set[ImportStatus]] = {
    "ck_import_jobs_status_allowed": set(ImportStatus),
    "ck_import_jobs_error_code_iff_failed": {ImportStatus.FAILED},
    "ck_import_jobs_started_at_iff_not_pending": {ImportStatus.PENDING},
    "ck_import_jobs_finished_at_iff_finished": {ImportStatus.COMPLETED, ImportStatus.FAILED},
}


async def test_every_status_check_is_listed(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE contype = 'c' AND conrelid = 'import_jobs'::regclass"
        )
    )
    mentioning_status = {name for name, definition in rows if re.search(r"\bstatus\b", definition)}

    assert mentioning_status == set(STATUS_CHECKS)


async def test_active_file_index_predicate_matches_the_domain(session: AsyncSession) -> None:
    definition = await session.scalar(
        text("SELECT pg_get_indexdef('uq_import_jobs_active_file_sha256'::regclass)")
    )
    assert definition is not None
    assert "UNIQUE" in definition
    assert set(re.findall(r"'([^']*)'::text", definition)) == {str(ImportStatus.FAILED)}


@pytest.mark.parametrize(("constraint", "statuses"), STATUS_CHECKS.items())
async def test_status_literals_in_checks_match_the_domain(
    session: AsyncSession, constraint: str, statuses: set[ImportStatus]
) -> None:
    assert await _literals(session, constraint) == {str(status) for status in statuses}
