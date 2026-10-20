"""Validation and normalization of non-money fields."""

import re
import unicodedata
from datetime import date

from transaction_analytics.domain.errors import FieldErrorCode, FieldValidationError

MAX_DESCRIPTION_LENGTH = 500

# Strict ISO date. date.fromisoformat alone also accepts "20261015" and "2026-W42-4". [0-9], not
# \d: in a str pattern \d matches every Unicode digit (Arabic-Indic, full-width, ...).
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WHITESPACE = re.compile(r"\s+")


def is_iso_date_text(text: str) -> bool:
    """YYYY-MM-DD in ASCII digits: the one date format (statements, ADR 0002; queries, ADR 0008).
    Says nothing about whether it is a real calendar date."""
    return _DATE_PATTERN.fullmatch(text) is not None


def parse_booking_date(raw: str) -> date:
    text = raw.strip()
    if not text:
        raise FieldValidationError(FieldErrorCode.MISSING_VALUE, "Date is required.")
    if not is_iso_date_text(text):
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
    """Description as shown to the user: Unicode-normalized, whitespace collapsed, case kept.

    Control characters (Unicode category Cc) left after whitespace handling are rejected:
    PostgreSQL TEXT cannot store NUL at all, and the rest are invisible junk. Format characters
    (Cf: zero-width joiners in emoji, RTL marks) are legitimate text and are kept.
    """
    text = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", raw)).strip()
    if not text:
        raise FieldValidationError(FieldErrorCode.MISSING_VALUE, "Description is required.")
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise FieldValidationError(
            FieldErrorCode.INVALID_CHARACTERS, "Description contains control characters."
        )
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise FieldValidationError(
            FieldErrorCode.DESCRIPTION_TOO_LONG,
            f"Description is longer than {MAX_DESCRIPTION_LENGTH} characters.",
        )
    return text


def normalize_description(cleaned: str) -> str:
    """Matching key for deduplication and grouping. Not shown to users."""
    return cleaned.casefold()
