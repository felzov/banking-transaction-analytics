"""Domain error codes.

Error messages are deliberately generic and never echo raw input values: statement rows contain
personal financial data, and these messages will end up in API responses and logs.
"""

from enum import StrEnum


class FieldErrorCode(StrEnum):
    """Why a single field of a row is invalid."""

    MISSING_VALUE = "missing_value"
    INVALID_AMOUNT_FORMAT = "invalid_amount_format"
    AMOUNT_TOO_PRECISE = "amount_too_precise"
    AMOUNT_OUT_OF_RANGE = "amount_out_of_range"
    INVALID_DATE = "invalid_date"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    DESCRIPTION_TOO_LONG = "description_too_long"
    INVALID_CHARACTERS = "invalid_characters"
    COLUMN_COUNT_MISMATCH = "column_count_mismatch"


class FileErrorCode(StrEnum):
    """Why the whole file is rejected before any row is accepted."""

    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_ENCODING = "invalid_encoding"
    MALFORMED_CSV = "malformed_csv"
    MISSING_COLUMNS = "missing_columns"
    DUPLICATE_COLUMNS = "duplicate_columns"
    TOO_MANY_ROWS = "too_many_rows"
    NO_DATA_ROWS = "no_data_rows"


class FieldValidationError(ValueError):
    """A single value failed validation. Carries a stable code, not the raw value."""

    def __init__(self, code: FieldErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FileRejectedError(Exception):
    """The file as a whole cannot be processed."""

    def __init__(self, code: FileErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
