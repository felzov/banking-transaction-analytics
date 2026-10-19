"""Account name rules: the shared name rule (domain/labels.py), reported with account codes."""

from transaction_analytics.domain.errors import (
    AccountErrorCode,
    AccountValidationError,
    LabelErrorCode,
    LabelValidationError,
)
from transaction_analytics.domain.labels import MAX_LABEL_LENGTH, clean_label

MAX_ACCOUNT_NAME_LENGTH = MAX_LABEL_LENGTH

_ACCOUNT_ERRORS = {
    LabelErrorCode.REQUIRED: (AccountErrorCode.NAME_REQUIRED, "Account name is required."),
    LabelErrorCode.INVALID_CHARACTERS: (
        AccountErrorCode.NAME_INVALID_CHARACTERS,
        "Account name contains control characters.",
    ),
    LabelErrorCode.TOO_LONG: (
        AccountErrorCode.NAME_TOO_LONG,
        f"Account name is longer than {MAX_ACCOUNT_NAME_LENGTH} characters.",
    ),
}


def clean_account_name(raw: str) -> str:
    """Unicode-normalized, whitespace collapsed and trimmed, case kept (see clean_label)."""
    try:
        return clean_label(raw)
    except LabelValidationError as exc:
        code, message = _ACCOUNT_ERRORS[exc.code]
        raise AccountValidationError(code, message) from None
