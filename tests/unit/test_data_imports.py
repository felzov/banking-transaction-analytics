"""Batching of bulk inserts, observed through a recording session (no database needed)."""

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

from transaction_analytics.data import imports as imports_data
from transaction_analytics.domain.csv_parser import ParsedTransaction, RowError
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.money import Currency

ROWS = 2_500
# One INSERT call for 50 000 rows made SQLAlchemy prepare every parameter set in one step and
# stalled the event loop for ~0.4 s. Bounded calls let other requests run between them.
MAX_ROWS_PER_CALL = 1_000


class RecordingSession:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    async def execute(self, statement: Any, parameters: list[dict[str, Any]]) -> None:
        self.batch_sizes.append(len(parameters))


JOB = SimpleNamespace(owner_id=uuid.uuid4(), account_id=uuid.uuid4(), id=uuid.uuid4())


async def test_transactions_are_inserted_in_bounded_batches() -> None:
    session = RecordingSession()
    rows = [
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

    await imports_data.insert_transactions(session, job=JOB, transactions=rows)  # type: ignore[arg-type]

    assert sum(session.batch_sizes) == ROWS
    assert max(session.batch_sizes) <= MAX_ROWS_PER_CALL


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

    await imports_data.insert_transactions(session, job=JOB, transactions=[])  # type: ignore[arg-type]
    await imports_data.insert_row_errors(session, job=JOB, errors=[])  # type: ignore[arg-type]

    assert session.batch_sizes == []
