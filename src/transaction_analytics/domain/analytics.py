"""Values for listing and summarizing an account's transactions (ADR 0008).

Totals are always per currency: amounts in different currencies are never added together
(ADR 0001). Signs keep the stored convention: inflow is the sum of positive amounts, outflow the
sum of negative ones (so it is <= 0), and net = inflow + outflow. A zero amount is counted but is
in neither sum. Sums are plain ints: they can exceed the BIGINT range of a single amount.
"""

import base64
import uuid
from dataclasses import dataclass
from datetime import date

from transaction_analytics.domain.errors import AnalyticsErrorCode, AnalyticsValidationError
from transaction_analytics.domain.money import Currency

# A canonical cursor is 58 characters; anything much longer is refused before decoding.
MAX_CURSOR_LENGTH = 64

_CURSOR_SEPARATOR = "/"


@dataclass(frozen=True, slots=True)
class DateRange:
    """Booking dates from start to end, both inclusive. Either end may be open (None)."""

    start: date | None = None
    end: date | None = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise AnalyticsValidationError(
                AnalyticsErrorCode.INVALID_DATE_RANGE, "'from' must not be after 'to'."
            )


@dataclass(frozen=True, slots=True)
class TransactionCursor:
    """Position after the last transaction of a page, in listing order (booked_on, id), newest
    first.

    Only a position: the query that uses it is still scoped by owner and account, so a forged or
    foreign cursor can at most skip rows of the caller's own account.
    """

    booked_on: date
    id: uuid.UUID

    def encode(self) -> str:
        text = f"{self.booked_on.isoformat()}{_CURSOR_SEPARATOR}{self.id.hex}"
        return base64.urlsafe_b64encode(text.encode("ascii")).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, text: str) -> "TransactionCursor":
        """Raises AnalyticsValidationError for anything encode() did not produce."""
        if len(text) > MAX_CURSOR_LENGTH or not text.isascii():
            raise _invalid_cursor()
        try:
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("ascii")
            day, _, hex_id = raw.partition(_CURSOR_SEPARATOR)
            cursor = cls(booked_on=date.fromisoformat(day), id=uuid.UUID(hex=hex_id))
        except ValueError:  # includes binascii.Error and UnicodeDecodeError
            raise _invalid_cursor() from None
        # The decoders above are lenient (they skip stray characters, accept "20261015" and UUIDs
        # in braces); only the exact form encode() produces is a cursor.
        if cursor.encode() != text:
            raise _invalid_cursor()
        return cursor


def _invalid_cursor() -> AnalyticsValidationError:
    return AnalyticsValidationError(AnalyticsErrorCode.INVALID_CURSOR, "Cursor is invalid.")


@dataclass(frozen=True, slots=True)
class CurrencyTotals:
    currency: Currency
    transactions: int
    # Sum of the positive amounts (>= 0), in minor units.
    inflow_minor: int
    # Sum of the negative amounts (<= 0), in minor units.
    outflow_minor: int

    @property
    def net_minor(self) -> int:
        return self.inflow_minor + self.outflow_minor


@dataclass(frozen=True, slots=True)
class MonthTotals:
    # The first day of the month.
    month: date
    totals: CurrencyTotals
