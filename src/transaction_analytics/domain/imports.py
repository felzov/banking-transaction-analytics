"""Import job lifecycle values and the outcome policy (ADR 0004).

Stored as TEXT with CHECK constraints (ADR 0003). The migration repeats these values as literals
on purpose; tests/integration/test_constraints.py checks that the two stay in sync.
"""

from dataclasses import dataclass
from enum import StrEnum

from transaction_analytics.domain.csv_parser import ParseResult
from transaction_analytics.domain.errors import FileRejectedError


class ImportStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportFailureCode(StrEnum):
    """Why an import job failed: every FileErrorCode plus a catch-all for unexpected errors.

    Kept as an explicit list (not generated from FileErrorCode) so it reads like the CHECK
    constraint it mirrors; a unit test asserts it covers every FileErrorCode.
    The message stored next to the code is fixed text, never an exception string.
    """

    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_ENCODING = "invalid_encoding"
    MALFORMED_CSV = "malformed_csv"
    MISSING_COLUMNS = "missing_columns"
    DUPLICATE_COLUMNS = "duplicate_columns"
    TOO_MANY_ROWS = "too_many_rows"
    NO_DATA_ROWS = "no_data_rows"
    INTERNAL_ERROR = "internal_error"


INTERNAL_ERROR_MESSAGE = "The import could not be processed. Please try again later."


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """Final state of an import job, known before anything is written (ADR 0004)."""

    status: ImportStatus
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    error_code: ImportFailureCode | None = None
    error_message: str | None = None


def completed_outcome(result: ParseResult) -> ImportOutcome:
    """Partial import: valid rows are kept and every invalid row is reported.

    There is deliberately no threshold. Even a file whose rows are all invalid completes, with
    valid_rows == 0, so the user sees every row error instead of one generic failure.
    """
    return ImportOutcome(
        status=ImportStatus.COMPLETED,
        total_rows=result.total_rows,
        valid_rows=len(result.transactions),
        invalid_rows=len(result.errors),
    )


def rejected_outcome(error: FileRejectedError) -> ImportOutcome:
    return ImportOutcome(
        status=ImportStatus.FAILED,
        error_code=ImportFailureCode(error.code.value),
        error_message=error.message,
    )


def internal_error_outcome() -> ImportOutcome:
    return ImportOutcome(
        status=ImportStatus.FAILED,
        error_code=ImportFailureCode.INTERNAL_ERROR,
        error_message=INTERNAL_ERROR_MESSAGE,
    )
