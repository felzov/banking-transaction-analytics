"""Batching of bulk inserts, observed through a recording session (no database needed)."""

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from transaction_analytics.data import imports as imports_data
from transaction_analytics.data.models import DEDUP_CONSTRAINT
from transaction_analytics.domain.csv_parser import ParsedTransaction, RowError
from transaction_analytics.domain.dedup import assign_occurrences
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.money import Currency

ROWS = 2_500
# One INSERT call for 50 000 rows made SQLAlchemy prepare every parameter set in one step and
# stalled the event loop for ~0.4 s. Bounded calls let other requests run between them.
MAX_ROWS_PER_CALL = 1_000


class RecordingSession:
    """Records each call; RETURNING yields every row except the first `skip_per_batch`."""

    def __init__(self, skip_per_batch: int = 0) -> None:
        self.batch_sizes: list[int] = []
        self.statements: list[Any] = []
        self.skip_per_batch = skip_per_batch

    async def execute(self, statement: Any, parameters: list[dict[str, Any]]) -> Any:
        self.batch_sizes.append(len(parameters))
        self.statements.append(statement)
        returned = parameters[self.skip_per_batch :]
        return SimpleNamespace(all=lambda: returned)


JOB = SimpleNamespace(owner_id=uuid.uuid4(), account_id=uuid.uuid4(), id=uuid.uuid4())
ROWS_TO_INSERT = assign_occurrences(
    [
        ParsedTransaction(
            row_number=index + 2,
            booked_on=date(2026, 9, 1),
            description="Row",
            normalized_description="row",
            amount_minor=-100,
            currency=Currency.GBP,
        )
        for index in range(ROWS)
    ]
)


async def test_transactions_are_inserted_in_bounded_batches() -> None:
    session = RecordingSession()

    inserted = await imports_data.insert_transactions(session, job=JOB, rows=ROWS_TO_INSERT)  # type: ignore[arg-type]

    assert sum(session.batch_sizes) == ROWS
    assert max(session.batch_sizes) <= MAX_ROWS_PER_CALL
    assert inserted == ROWS


async def test_inserted_count_adds_up_rows_returned_by_every_batch() -> None:
    session = RecordingSession(skip_per_batch=10)

    inserted = await imports_data.insert_transactions(session, job=JOB, rows=ROWS_TO_INSERT)  # type: ignore[arg-type]

    batches = -(-ROWS // imports_data.INSERT_BATCH_SIZE)  # ceiling division
    assert len(session.batch_sizes) == batches
    assert inserted == ROWS - batches * 10


async def test_only_the_dedup_constraint_is_skipped() -> None:
    # Any other violation (row number, foreign keys, CHECKs) must still raise.
    session = RecordingSession()

    await imports_data.insert_transactions(session, job=JOB, rows=ROWS_TO_INSERT[:1])  # type: ignore[arg-type]

    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert f"ON CONFLICT ON CONSTRAINT {DEDUP_CONSTRAINT} DO NOTHING" in sql
    assert "RETURNING" in sql


async def test_rows_carry_their_dedup_key() -> None:
    captured: list[dict[str, Any]] = []

    class CapturingSession(RecordingSession):
        async def execute(self, statement: Any, parameters: list[dict[str, Any]]) -> Any:
            captured.extend(parameters)
            return await super().execute(statement, parameters)

    await imports_data.insert_transactions(CapturingSession(), job=JOB, rows=ROWS_TO_INSERT[:3])  # type: ignore[arg-type]

    assert [(p["fingerprint"], p["occurrence"]) for p in captured] == [
        (row.fingerprint, row.occurrence) for row in ROWS_TO_INSERT[:3]
    ]
    assert [p["occurrence"] for p in captured] == [1, 2, 3]


async def test_row_errors_are_inserted_in_bounded_batches() -> None:
    session = RecordingSession()
    errors = [
        RowError(index + 2, FieldErrorCode.INVALID_DATE, "date", "Date is not a real date.")
        for index in range(ROWS)
    ]

    await imports_data.insert_row_errors(session, job=JOB, errors=errors)  # type: ignore[arg-type]

    assert sum(session.batch_sizes) == ROWS
    assert max(session.batch_sizes) <= MAX_ROWS_PER_CALL


async def test_nothing_to_insert_issues_no_statement() -> None:
    session = RecordingSession()

    assert await imports_data.insert_transactions(session, job=JOB, rows=[]) == 0  # type: ignore[arg-type]
    await imports_data.insert_row_errors(session, job=JOB, errors=[])  # type: ignore[arg-type]

    assert session.batch_sizes == []


async def test_rows_are_inserted_in_key_order_not_file_order() -> None:
    # Concurrent imports wait on each other's uncommitted rows; one global order (fingerprint,
    # occurrence) means two imports of overlapping files cannot deadlock (ADR 0006).
    captured: list[dict[str, Any]] = []

    class CapturingSession(RecordingSession):
        async def execute(self, statement: Any, parameters: list[dict[str, Any]]) -> Any:
            captured.extend(parameters)
            return await super().execute(statement, parameters)

    file_rows = assign_occurrences(
        [
            ParsedTransaction(
                row_number=index + 2,
                booked_on=date(2026, 9, 1),
                description="Row",
                normalized_description=f"row {index % 7}",
                amount_minor=-100,
                currency=Currency.GBP,
            )
            for index in range(2 * MAX_ROWS_PER_CALL + 50)  # spans batches
        ]
    )

    await imports_data.insert_transactions(CapturingSession(), job=JOB, rows=file_rows)  # type: ignore[arg-type]

    keys = [(p["fingerprint"], p["occurrence"]) for p in captured]
    assert keys == sorted((row.fingerprint, row.occurrence) for row in file_rows)
    assert keys != [(row.fingerprint, row.occurrence) for row in file_rows]
