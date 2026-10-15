"""Currency and amount parsing.

Policy (ADR 0002):
- Amounts are stored as signed integers in the currency's minor unit (pence, cents).
- Negative = money leaving the account (debit), positive = money arriving (credit).
- Parsing never rounds: more fractional digits than the currency allows is an error.
"""

import re
from enum import StrEnum

from transaction_analytics.domain.errors import FieldErrorCode, FieldValidationError


class Currency(StrEnum):
    GBP = "GBP"
    EUR = "EUR"
    USD = "USD"
    JPY = "JPY"


MINOR_UNIT_EXPONENTS: dict[Currency, int] = {
    Currency.GBP: 2,
    Currency.EUR: 2,
    Currency.USD: 2,
    Currency.JPY: 0,
}

# PostgreSQL BIGINT range. Checked here so an oversized value becomes a row error, not a DB error.
MAX_ABS_AMOUNT_MINOR = 2**63 - 1

# Plain decimal only: optional sign, digits, optional fraction. Rejects "1,000", "1e3", "NaN",
# "Infinity", "1_000", "£10", ".5", "5." and surrounding junk.
_AMOUNT_PATTERN = re.compile(r"[+-]?\d+(?:\.\d+)?")
_MAX_AMOUNT_TEXT_LENGTH = 40


def parse_currency(raw: str) -> Currency:
    code = raw.strip().upper()
    if not code:
        raise FieldValidationError(FieldErrorCode.MISSING_VALUE, "Currency is required.")
    try:
        return Currency(code)
    except ValueError:
        raise FieldValidationError(
            FieldErrorCode.UNSUPPORTED_CURRENCY,
            "Currency is not supported. Expected an ISO 4217 code such as GBP or EUR.",
        ) from None


def parse_amount_minor(raw: str, currency: Currency) -> int:
    """Convert a decimal string such as "-12.30" to signed minor units (-1230)."""
    text = raw.strip()
    if not text:
        raise FieldValidationError(FieldErrorCode.MISSING_VALUE, "Amount is required.")
    if len(text) > _MAX_AMOUNT_TEXT_LENGTH or _AMOUNT_PATTERN.fullmatch(text) is None:
        raise FieldValidationError(
            FieldErrorCode.INVALID_AMOUNT_FORMAT,
            "Amount must be a plain decimal number like -12.30 (no symbols or separators).",
        )

    exponent = MINOR_UNIT_EXPONENTS[currency]
    whole, _, fraction = text.lstrip("+-").partition(".")
    if len(fraction) > exponent:
        raise FieldValidationError(
            FieldErrorCode.AMOUNT_TOO_PRECISE,
            f"Amount has more than {exponent} decimal places allowed for {currency}.",
        )

    # Exact integer arithmetic on the digits; no float, no Decimal context precision/rounding.
    minor = int(whole + fraction.ljust(exponent, "0"))
    if text.startswith("-"):
        minor = -minor
    if abs(minor) > MAX_ABS_AMOUNT_MINOR:
        raise FieldValidationError(
            FieldErrorCode.AMOUNT_OUT_OF_RANGE, "Amount is outside the supported range."
        )
    return minor
