"""Transactions from banking-ledger-api (ADR 0009).

A ledger account's history is read from the ledger's statement: entries newest first, each with
its own signed amount and the account's balance right after it. The ledger is another service, so
everything it returns is checked like a file is (nothing is stored on trust), and the checks below
are pure: the HTTP client (integrations/ledger.py) only turns JSON into these values.

- Every entry becomes one transaction, booked on the UTC date of its posting time, described by
  fixed text from its kind and sign (the ledger has no descriptions), in the ledger account's
  currency.
- Deduplication is exact: a ledger entry has a stable id, so its fingerprint is a hash of that id
  and its occurrence is always 1. uq_transactions_dedup then holds each entry once per account,
  and a CHECK in migration 0006 recomputes the same bytes in SQL.
- The statement must be internally consistent: balances chain from entry to entry, the newest
  entry's balance is the statement's balance, and a history read to its start begins at zero.
"""

import hashlib
import itertools
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from transaction_analytics.domain.errors import LedgerErrorCode, LedgerValidationError
from transaction_analytics.domain.fields import normalize_description
from transaction_analytics.domain.money import MAX_ABS_AMOUNT_MINOR, Currency


class AccountSource(StrEnum):
    """Where an account's transactions come from. Fixed when the account is created."""

    CSV = "csv"
    LEDGER = "ledger"


class LedgerEntryKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"


# The ledger's currencies and their minor-unit exponents (banking-ledger-api domain/currency.py).
# Its amount_minor is stored as it is, which is exact only where its exponent is ours (ADR 0001;
# tests/unit/test_ledger.py compares the two). A currency missing here is refused, not guessed:
# its minor unit at the ledger is unknown.
LEDGER_MINOR_UNIT_EXPONENTS: Mapping[Currency, int] = {Currency.GBP: 2, Currency.EUR: 2}

# banking-ledger-api's bearer token: "lt_" and 43 base64url characters (256 random bits). Checked
# before it is sent: a header value outside this shape (e.g. non-Latin-1 text) would fail inside
# the HTTP client rather than at the ledger.
_LEDGER_TOKEN = re.compile(r"lt_[A-Za-z0-9_-]{43}")

# Fixed text per kind and direction; the ledger has no descriptions and none come from input.
_DESCRIPTIONS = {
    (LedgerEntryKind.DEPOSIT, True): "Ledger deposit",
    (LedgerEntryKind.WITHDRAWAL, False): "Ledger withdrawal",
    (LedgerEntryKind.TRANSFER, True): "Ledger transfer in",
    (LedgerEntryKind.TRANSFER, False): "Ledger transfer out",
}

# Unit separator, as in domain/dedup.py. A CSV key starts with an ISO date, a ledger key with
# "ledger", so the two kinds of key never serialize to the same string.
_FINGERPRINT_PREFIX = "ledger\x1f"


@dataclass(frozen=True, slots=True)
class LedgerLink:
    """The banking-ledger-api user and account a ledger account reads from."""

    user_id: uuid.UUID
    account_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One entry of a ledger statement, as the ledger reported it (shape already checked)."""

    entry_id: uuid.UUID
    kind: LedgerEntryKind
    # Signed for this account: negative debits it, positive credits it (the convention of ADR 0001).
    amount_minor: int
    balance_after_minor: int
    # Timezone-aware: the posting transaction's start time.
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerPage:
    # The account's current balance, from the same snapshot as the entries.
    balance_minor: int
    # Newest first.
    entries: list[LedgerEntry]
    next_cursor: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    """A ledger entry ready to store."""

    entry_id: uuid.UUID
    booked_on: date
    description: str
    normalized_description: str
    amount_minor: int
    currency: Currency
    fingerprint: bytes


def check_ledger_token(token: str) -> None:
    if _LEDGER_TOKEN.fullmatch(token) is None:
        raise LedgerValidationError(
            LedgerErrorCode.INVALID_LEDGER_TOKEN,
            "Ledger token must be a banking-ledger-api access token (lt_...).",
        )


def ledger_fingerprint(entry_id: uuid.UUID) -> bytes:
    """SHA-256 of "ledger", a unit separator and the entry id in canonical lowercase form.

    Migration 0006 checks every ledger row against the same bytes computed in SQL
    (`sha256(convert_to('ledger' || chr(31) || ledger_entry_id::text, 'UTF8'))`): change both or
    neither.
    """
    return hashlib.sha256((_FINGERPRINT_PREFIX + str(entry_id)).encode("utf-8")).digest()


def to_transaction(entry: LedgerEntry, currency: Currency) -> LedgerTransaction:
    description = _DESCRIPTIONS[(entry.kind, entry.amount_minor > 0)]
    return LedgerTransaction(
        entry_id=entry.entry_id,
        booked_on=_booking_day(entry.created_at),
        description=description,
        normalized_description=normalize_description(description),
        amount_minor=entry.amount_minor,
        currency=currency,
        fingerprint=ledger_fingerprint(entry.entry_id),
    )


def _booking_day(created_at: datetime) -> date:
    # A posting time is an instant; its booking day is the UTC date, whatever the ledger's or the
    # caller's time zone (ADR 0009). OverflowError for an instant whose UTC date is outside years
    # 1-9999; _check_entry refuses those first.
    return created_at.astimezone(UTC).date()


class LedgerResponseError(Exception):
    """What the ledger returned is inconsistent. Fixed text only: never a value it sent."""


def check_statement(pages: Sequence[LedgerPage], *, reached_start: bool) -> list[LedgerEntry]:
    """The entries of consecutive statement pages, newest first, after checking they agree.

    `reached_start` says the last page had no next_cursor, i.e. the whole history was read; only
    then can the oldest entry be checked against an opening balance of zero.

    Raises LedgerResponseError.
    """
    entries = [entry for page in pages for entry in page.entries]
    for page in pages[:-1]:
        if not page.entries:
            # A page that is empty but points further would let the ledger page forever.
            raise LedgerResponseError("A statement page with a cursor has no entries.")
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise LedgerResponseError("A ledger entry appears more than once.")
    for entry in entries:
        _check_entry(entry)
    if pages and entries and pages[0].balance_minor != entries[0].balance_after_minor:
        raise LedgerResponseError("The newest entry does not end at the statement balance.")
    for newer, older in itertools.pairwise(entries):
        if newer.balance_after_minor - newer.amount_minor != older.balance_after_minor:
            raise LedgerResponseError("Entry balances do not follow from each other.")
    if reached_start:
        opening = (
            entries[-1].balance_after_minor - entries[-1].amount_minor
            if entries
            else (pages[0].balance_minor if pages else 0)
        )
        if opening != 0:
            raise LedgerResponseError("The account history does not start at zero.")
    return entries


def _check_entry(entry: LedgerEntry) -> None:
    if entry.amount_minor == 0 or abs(entry.amount_minor) > MAX_ABS_AMOUNT_MINOR:
        raise LedgerResponseError("A ledger amount is zero or outside the supported range.")
    if (entry.kind, entry.amount_minor > 0) not in _DESCRIPTIONS:
        # A deposit always credits the account and a withdrawal always debits it.
        raise LedgerResponseError("A ledger amount has the wrong sign for its kind.")
    if entry.created_at.tzinfo is None:
        raise LedgerResponseError("A ledger posting time has no time zone.")
    try:
        _booking_day(entry.created_at)
    except OverflowError:
        # E.g. 9999-12-31T23:59:59-01:00: a valid time whose UTC date does not exist.
        raise LedgerResponseError("A ledger posting time has no UTC calendar date.") from None
