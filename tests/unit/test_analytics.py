import base64
import uuid
from datetime import date

import pytest

from transaction_analytics.domain.analytics import (
    MAX_CURSOR_LENGTH,
    CurrencyTotals,
    DateRange,
    TransactionCursor,
)
from transaction_analytics.domain.errors import AnalyticsErrorCode, AnalyticsValidationError
from transaction_analytics.domain.money import Currency

MARKER = "LEAK-ME-4417"
ID = uuid.UUID("0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- DateRange -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, None),
        (date(2026, 9, 1), None),
        (None, date(2026, 9, 30)),
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 9, 1), date(2026, 9, 1)),  # one day: both ends are inclusive
    ],
)
def test_valid_date_ranges(start: date | None, end: date | None) -> None:
    dates = DateRange(start=start, end=end)

    assert (dates.start, dates.end) == (start, end)


def test_start_after_end_is_rejected() -> None:
    with pytest.raises(AnalyticsValidationError) as exc:
        DateRange(start=date(2026, 9, 2), end=date(2026, 9, 1))

    assert exc.value.code is AnalyticsErrorCode.INVALID_DATE_RANGE


# --- TransactionCursor ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        TransactionCursor(booked_on=date(2026, 9, 1), id=ID),
        TransactionCursor(booked_on=date(1, 1, 1), id=uuid.UUID(int=0)),
        TransactionCursor(booked_on=date(99, 1, 1), id=ID),
        TransactionCursor(booked_on=date(9999, 12, 31), id=uuid.UUID(int=2**128 - 1)),
    ],
)
def test_cursor_round_trips(cursor: TransactionCursor) -> None:
    text = cursor.encode()

    assert TransactionCursor.decode(text) == cursor
    assert len(text) <= MAX_CURSOR_LENGTH
    assert text.isascii() and "=" not in text


@pytest.mark.parametrize(
    "text",
    [
        "",
        MARKER,
        "!!!!",
        "é" * 10,
        "A" * (MAX_CURSOR_LENGTH + 1),
        _b64(f"2026-09-01{ID.hex}"),  # no separator
        _b64(f"2026-09-31/{ID.hex}"),  # not a real date
        _b64(f"20260901/{ID.hex}"),  # date.fromisoformat accepts it; encode() never writes it
        _b64(f"2026-09-01/{ID}"),  # hyphenated UUID
        _b64(f"2026-09-01/{{{ID.hex}}}"),  # UUID in braces
        _b64(f"2026-09-01/{ID.hex.upper()}"),
        _b64(f"2026-09-01/{ID.hex}/extra"),
        _b64("2026-09-01/"),
        TransactionCursor(booked_on=date(2026, 9, 1), id=ID).encode() + "==",  # padding
        TransactionCursor(booked_on=date(2026, 9, 1), id=ID).encode() + ".",  # stray character
        base64.b64encode(f"2026-09-01/{ID.hex}".encode()).decode(),  # standard alphabet, padded
    ],
)
def test_malformed_cursor_is_rejected_without_echo(text: str) -> None:
    with pytest.raises(AnalyticsValidationError) as exc:
        TransactionCursor.decode(text)

    assert exc.value.code is AnalyticsErrorCode.INVALID_CURSOR
    assert MARKER not in exc.value.message
    assert exc.value.__cause__ is None  # nothing of the input survives in a chained exception


# --- CurrencyTotals ------------------------------------------------------------------------------


def test_net_is_inflow_plus_outflow() -> None:
    totals = CurrencyTotals(
        currency=Currency.GBP, transactions=3, inflow_minor=250_000, outflow_minor=-95_320
    )

    assert totals.net_minor == 154_680


def test_net_is_exact_beyond_bigint() -> None:
    big = 2 * (2**63 - 1)
    totals = CurrencyTotals(
        currency=Currency.USD, transactions=4, inflow_minor=big, outflow_minor=-big + 1
    )

    assert totals.net_minor == 1
