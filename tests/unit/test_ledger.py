import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from transaction_analytics.domain.errors import LedgerErrorCode, LedgerValidationError
from transaction_analytics.domain.ledger import (
    LEDGER_MINOR_UNIT_EXPONENTS,
    LedgerEntry,
    LedgerEntryKind,
    LedgerPage,
    LedgerResponseError,
    check_ledger_token,
    check_statement,
    ledger_fingerprint,
    to_transaction,
)
from transaction_analytics.domain.money import MAX_ABS_AMOUNT_MINOR, MINOR_UNIT_EXPONENTS, Currency

DEPOSIT, WITHDRAWAL, TRANSFER = LedgerEntryKind
POSTED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def history(*postings: tuple[LedgerEntryKind, int]) -> list[LedgerEntry]:
    """Entries for postings given oldest first, returned newest first like a statement."""
    entries, balance = [], 0
    for index, (kind, amount) in enumerate(postings):
        balance += amount
        entries.append(
            LedgerEntry(
                entry_id=uuid.UUID(int=index + 1),
                kind=kind,
                amount_minor=amount,
                balance_after_minor=balance,
                created_at=POSTED + timedelta(days=index),
            )
        )
    return entries[::-1]


def page(entries: list[LedgerEntry], *, cursor: bool = False) -> LedgerPage:
    balance = entries[0].balance_after_minor if entries else 0
    next_cursor = entries[-1].entry_id if cursor else None
    return LedgerPage(balance_minor=balance, entries=entries, next_cursor=next_cursor)


THREE = history((DEPOSIT, 2500), (WITHDRAWAL, -1000), (TRANSFER, -500))


# Tokens


def test_ledger_token_shape_is_accepted() -> None:
    check_ledger_token("lt_" + "A" * 40 + "-_9")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "lt_" + "A" * 42,
        "lt_" + "A" * 44,
        "bta_" + "A" * 43,  # this service's own token, sent by mistake
        "lt_" + "A" * 42 + "=",
        "lt_" + "é" * 43,
        " lt_" + "A" * 43,
        "lt_" + "A" * 43 + "\n",
    ],
)
def test_other_token_shapes_are_rejected(token: str) -> None:
    with pytest.raises(LedgerValidationError) as exc:
        check_ledger_token(token)

    assert exc.value.code == LedgerErrorCode.INVALID_LEDGER_TOKEN


# Mapping an entry to a transaction


@pytest.mark.parametrize(
    ("kind", "amount", "description"),
    [
        (DEPOSIT, 2500, "Ledger deposit"),
        (WITHDRAWAL, -1000, "Ledger withdrawal"),
        (TRANSFER, 700, "Ledger transfer in"),
        (TRANSFER, -700, "Ledger transfer out"),
    ],
)
def test_entry_becomes_a_transaction(kind: LedgerEntryKind, amount: int, description: str) -> None:
    entry = LedgerEntry(uuid.UUID(int=7), kind, amount, amount, POSTED)

    row = to_transaction(entry, Currency.EUR)

    assert row.entry_id == entry.entry_id
    assert row.description == description
    assert row.normalized_description == description.casefold()
    assert (row.amount_minor, row.currency) == (amount, Currency.EUR)
    assert row.booked_on == date(2026, 9, 1)
    assert row.fingerprint == ledger_fingerprint(entry.entry_id)


@pytest.mark.parametrize(
    ("created_at", "booked_on"),
    [
        # 23:30 at UTC-1 is 00:30 UTC the next day: the booking day is the UTC date.
        (datetime(2026, 9, 30, 23, 30, tzinfo=timezone(timedelta(hours=-1))), date(2026, 10, 1)),
        (datetime(2026, 10, 1, 0, 30, tzinfo=timezone(timedelta(hours=2))), date(2026, 9, 30)),
        (datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC), date(2026, 9, 30)),
    ],
)
def test_booking_day_is_the_utc_date(created_at: datetime, booked_on: date) -> None:
    entry = LedgerEntry(uuid.UUID(int=7), DEPOSIT, 100, 100, created_at)

    assert to_transaction(entry, Currency.GBP).booked_on == booked_on


def test_ledger_fingerprint_is_pinned() -> None:
    # Migration 0006 recomputes these bytes in a CHECK; changing the format breaks stored rows.
    entry_id = uuid.UUID("0190a3f0-0000-7000-8000-000000000001")

    assert ledger_fingerprint(entry_id).hex() == (
        "6c0dbb29b8ef6995de27efe0e90372d5b0e82f35d513e2a17ec4c74c776c57fd"
    )


def test_ledger_fingerprint_differs_per_entry() -> None:
    assert ledger_fingerprint(uuid.UUID(int=1)) != ledger_fingerprint(uuid.UUID(int=2))
    assert len(ledger_fingerprint(uuid.UUID(int=1))) == 32


# Checking a statement


def test_complete_history_is_accepted_newest_first() -> None:
    assert check_statement([page(THREE)], reached_start=True) == THREE


def test_history_across_pages_is_accepted() -> None:
    pages = [page(THREE[:2], cursor=True), page(THREE[2:])]
    pages[1] = dataclasses.replace(pages[1], balance_minor=pages[0].balance_minor)

    assert check_statement(pages, reached_start=True) == THREE


def test_empty_account_is_accepted() -> None:
    assert check_statement([page([])], reached_start=True) == []


def test_partial_history_is_not_checked_against_zero() -> None:
    # An incremental sync stops at stored entries: the oldest read entry need not start at zero.
    newest_two = THREE[:2]

    assert check_statement([page(newest_two, cursor=True)], reached_start=False) == newest_two


def test_broken_balance_chain_is_rejected() -> None:
    broken = [THREE[0], dataclasses.replace(THREE[1], balance_after_minor=1501), THREE[2]]

    with pytest.raises(LedgerResponseError):
        check_statement([page(broken)], reached_start=True)


def test_statement_balance_must_match_the_newest_entry() -> None:
    with pytest.raises(LedgerResponseError):
        check_statement([dataclasses.replace(page(THREE), balance_minor=999)], reached_start=True)


def test_complete_history_must_start_at_zero() -> None:
    # Consistent chain, but the oldest entry does not start from an empty account: a page is
    # missing, or the ledger lost history.
    with pytest.raises(LedgerResponseError):
        check_statement([page(THREE[:2])], reached_start=True)


def test_empty_account_must_have_zero_balance() -> None:
    with pytest.raises(LedgerResponseError):
        check_statement(
            [LedgerPage(balance_minor=5, entries=[], next_cursor=None)], reached_start=True
        )


def test_repeated_entry_is_rejected() -> None:
    with pytest.raises(LedgerResponseError):
        check_statement([page(THREE, cursor=True), page(THREE)], reached_start=True)


def test_empty_page_with_a_cursor_is_rejected() -> None:
    empty = LedgerPage(balance_minor=1000, entries=[], next_cursor=uuid.UUID(int=99))

    with pytest.raises(LedgerResponseError):
        check_statement([empty, page(THREE)], reached_start=True)


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        (DEPOSIT, -100),
        (WITHDRAWAL, 100),
        (DEPOSIT, 0),
        (TRANSFER, 0),
        (DEPOSIT, MAX_ABS_AMOUNT_MINOR + 1),
        (WITHDRAWAL, -MAX_ABS_AMOUNT_MINOR - 1),
    ],
)
def test_impossible_amount_is_rejected(kind: LedgerEntryKind, amount: int) -> None:
    entry = LedgerEntry(uuid.UUID(int=1), kind, amount, amount, POSTED)

    with pytest.raises(LedgerResponseError):
        check_statement([page([entry])], reached_start=True)


def test_largest_amount_is_accepted() -> None:
    entry = LedgerEntry(
        uuid.UUID(int=1), DEPOSIT, MAX_ABS_AMOUNT_MINOR, MAX_ABS_AMOUNT_MINOR, POSTED
    )

    assert check_statement([page([entry])], reached_start=True) == [entry]


def test_naive_posting_time_is_rejected() -> None:
    entry = LedgerEntry(uuid.UUID(int=1), DEPOSIT, 100, 100, datetime(2026, 9, 1, 12, 0))

    with pytest.raises(LedgerResponseError):
        check_statement([page([entry])], reached_start=True)


def test_errors_carry_no_values() -> None:
    marker = 987654321
    entry = LedgerEntry(uuid.UUID(int=1), DEPOSIT, 100, marker, POSTED)

    with pytest.raises(LedgerResponseError) as exc:
        check_statement([page([entry])], reached_start=True)

    assert str(marker) not in str(exc.value)


@pytest.mark.parametrize(
    "created_at",
    [
        datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-1))),
        datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_posting_time_without_a_utc_date_is_rejected(created_at: datetime) -> None:
    # Review fix: these passed the checks, then to_transaction raised OverflowError.
    entry = LedgerEntry(uuid.UUID(int=1), DEPOSIT, 100, 100, created_at)

    with pytest.raises(LedgerResponseError):
        check_statement([page([entry])], reached_start=True)


def test_ledger_currencies_have_our_minor_units() -> None:
    # Ledger amounts are stored as they are, which is exact only where the ledger's exponent is
    # ours (ADR 0001). The ledger's own table: banking-ledger-api domain/currency.py.
    assert LEDGER_MINOR_UNIT_EXPONENTS == {Currency.GBP: 2, Currency.EUR: 2}
    for currency, exponent in LEDGER_MINOR_UNIT_EXPONENTS.items():
        assert MINOR_UNIT_EXPONENTS[currency] == exponent, currency
