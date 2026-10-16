"""Import job lifecycle values.

Stored as TEXT with CHECK constraints (ADR 0003). The migration repeats these values as literals
on purpose; tests/integration/test_constraints.py checks that the two stay in sync.
"""

from enum import StrEnum


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
