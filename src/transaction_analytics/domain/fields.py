"""Validation and normalization of non-money fields."""

import re
import unicodedata
from datetime import date

from transaction_analytics.domain.errors import FieldErrorCode, FieldValidationError

MAX_DESCRIPTION_LENGTH = 500

# Strict ISO date. date.fromisoformat alone also accepts "20261015" and "2026-W42-4".
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_WHITESPACE = re.compile(r"\s+")


def parse_booking_date(raw: str) -> date:
    text = raw.strip()
    if not text:
        raise FieldValidationError(FieldErrorCode.MISSING_VALUE, "Date is required.")
    if _DATE_PATTERN.fullmatch(text) is None:
        raise FieldValidationError(
            FieldErrorCode.INVALID_DATE, "Date must be in YYYY-MM-DD format."
        )
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise FieldValidationError(
            FieldErrorCode.INVALID_DATE, "Date is not a real calendar date."
        ) from None


def clean_description(raw: str) -> str:
    """Description as shown to the user: Unicode-normalized, whitespace collapsed, case kept."""
    text = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", raw)).strip()
    if not text:
        raise FieldValidationError(FieldErrorCode.MISSING_VALUE, "Description is required.")
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise FieldValidationError(
            FieldErrorCode.DESCRIPTION_TOO_LONG,
            f"Description is longer than {MAX_DESCRIPTION_LENGTH} characters.",
        )
    return text


def normalize_description(cleaned: str) -> str:
    """Matching key for deduplication and grouping. Not shown to users."""
    return cleaned.casefold()
