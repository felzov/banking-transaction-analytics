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


class AccountErrorCode(StrEnum):
    """Why an account name is invalid. Separate from FieldErrorCode, which mirrors a CHECK."""

    NAME_REQUIRED = "name_required"
    NAME_TOO_LONG = "name_too_long"
    NAME_INVALID_CHARACTERS = "name_invalid_characters"


class LabelErrorCode(StrEnum):
    """Why a name for an account, user or API token is invalid (domain/labels.py)."""

    REQUIRED = "required"
    TOO_LONG = "too_long"
    INVALID_CHARACTERS = "invalid_characters"


class TokenErrorCode(StrEnum):
    """Why an API token cannot be issued as requested."""

    LIFETIME_OUT_OF_RANGE = "lifetime_out_of_range"


class AnalyticsErrorCode(StrEnum):
    """Why a transaction listing or summary query is invalid."""

    INVALID_DATE_RANGE = "invalid_date_range"
    INVALID_CURSOR = "invalid_cursor"


class LedgerErrorCode(StrEnum):
    """Why a ledger link or sync request is invalid."""

    INVALID_LEDGER_TOKEN = "invalid_ledger_token"


class CodedValidationError[C: StrEnum](ValueError):
    """Input failed validation. Carries a stable code and fixed text, never the raw value.

    The API answers every subclass with 422 (api/errors.py); the CLI prints the message.
    """

    def __init__(self, code: C, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FieldValidationError(CodedValidationError[FieldErrorCode]):
    """A single value of a statement row failed validation."""


class AccountValidationError(CodedValidationError[AccountErrorCode]):
    """An account name failed validation."""


class LabelValidationError(CodedValidationError[LabelErrorCode]):
    """A name (account, user, API token) failed validation."""


class TokenValidationError(CodedValidationError[TokenErrorCode]):
    """An API token request failed validation."""


class AnalyticsValidationError(CodedValidationError[AnalyticsErrorCode]):
    """A transaction listing or summary query failed validation."""


class LedgerValidationError(CodedValidationError[LedgerErrorCode]):
    """A ledger link or sync request failed validation."""


class FileRejectedError(Exception):
    """The file as a whole cannot be processed."""

    def __init__(self, code: FileErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
