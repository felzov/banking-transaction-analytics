"""Ledger sync: read a linked ledger account's statement and store its new entries (ADR 0009).

1. The ledger token's shape and the server's ledger configuration are checked before anything
   else; then the account must belong to the owner and be a ledger account.
2. No database transaction is open while the ledger is called: each page's "already stored?"
   query is its own short transaction (like the upload, ADR 0007 item 10).
3. The statement is read newest first and stops at the first page holding an entry the account
   already has. Syncs store all their entries or none, and the ledger's statement is in commit
   order with the prefix property (its ADR 0012: an entry never commits below a committed one), so
   the stored entries are always the oldest part of the history: everything older is stored too.
   Not created_at order: that could put a late-committing posting below entries already synced.
4. Everything read is checked (domain/ledger.py) before anything is written; an inconsistent
   statement stores nothing. Checking and mapping run in a worker thread: for 50 000 entries they
   blocked the event loop for ~0.26 s (like the CSV parse, ADR 0004).
5. One transaction inserts the sync and its entries. An entry the account already holds (e.g.
   stored by a concurrent sync) is skipped by uq_transactions_dedup and counted as a duplicate.
6. A sync that finds nothing new records nothing: a client polling for news would otherwise add
   a row per poll. That includes losing every new entry to a concurrent sync (rolled back).

The token is used for this sync's calls and then forgotten: it is never stored or logged.
"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data import ledger as ledger_data
from transaction_analytics.data.models import LedgerSync
from transaction_analytics.domain.ledger import (
    AccountSource,
    LedgerPage,
    LedgerResponseError,
    LedgerTransaction,
    check_ledger_token,
    check_statement,
    ledger_fingerprint,
    to_transaction,
)
from transaction_analytics.domain.money import Currency
from transaction_analytics.integrations import ledger as ledger_client
from transaction_analytics.services.errors import (
    AccountNotFoundError,
    AccountSourceMismatchError,
    LedgerAccountUnavailableError,
    LedgerInvalidResponseError,
    LedgerNotConfiguredError,
    LedgerSyncNotFoundError,
    LedgerTokenRejectedError,
    LedgerTooManyEntriesError,
    LedgerUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LedgerSettings:
    client: ledger_client.LedgerConfig
    sync_timeout_seconds: float
    max_entries: int
    # Replaces the network in tests (httpx2.MockTransport); None in the app.
    transport: httpx2.AsyncBaseTransport | None = None


@dataclass(frozen=True, slots=True)
class LedgerSyncResult:
    # None when nothing new was found: nothing was recorded (item 6).
    sync: LedgerSync | None
    fetched_entries: int
    duplicate_entries: int


@dataclass(frozen=True, slots=True)
class _Statement:
    currency: Currency
    pages: list[LedgerPage]
    # The last page had no next_cursor: the whole history was read.
    reached_start: bool
    # How many of the read entries the account already held when their page was read.
    known_entries: int


async def sync_account(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    token: str,
    ledger: LedgerSettings | None,
) -> LedgerSyncResult:
    """Store the ledger entries the account does not hold yet, and record the sync if any were.

    Owns the transaction boundaries, like import_statement: callers pass a session with no
    pending work of their own.

    Raises LedgerValidationError, LedgerNotConfiguredError, AccountNotFoundError,
    AccountSourceMismatchError, or one of the Ledger*Error service errors for the ledger's answer.
    """
    check_ledger_token(token)
    if ledger is None:
        raise LedgerNotConfiguredError
    account = await accounts_data.get_account(session, owner_id=owner_id, account_id=account_id)
    if account is None:
        raise AccountNotFoundError
    # The ids are set exactly for ledger accounts (ck_accounts_ledger_link_iff_ledger).
    if (
        account.source != AccountSource.LEDGER
        or account.ledger_user_id is None
        or account.ledger_account_id is None
    ):
        raise AccountSourceMismatchError
    ledger_user_id, ledger_account_id = account.ledger_user_id, account.ledger_account_id
    # End the read transaction: no pooled connection waits "idle in transaction" on the ledger.
    await session.rollback()

    try:
        statement = await _read_statement(
            session,
            ledger,
            token,
            owner_id=owner_id,
            account_id=account_id,
            ledger_user_id=ledger_user_id,
            ledger_account_id=ledger_account_id,
        )
        rows = await asyncio.to_thread(_checked_rows, statement)
    except ledger_client.LedgerError as exc:
        # The class says what happened; the text is fixed anyway, but nothing more is needed.
        logger.warning("Ledger sync failed: %s", type(exc).__name__)
        raise _service_error(exc) from None
    except LedgerResponseError:
        logger.warning("Ledger sync failed: inconsistent statement")
        raise LedgerInvalidResponseError from None

    if statement.known_entries == len(rows):
        return LedgerSyncResult(None, len(rows), len(rows))
    sync = await ledger_data.insert_sync(
        session, owner_id=owner_id, account_id=account_id, fetched_entries=len(rows)
    )
    inserted = await ledger_data.insert_ledger_transactions(session, sync=sync, rows=rows)
    if inserted == 0:
        # A concurrent sync stored them first: nothing new after all.
        await session.rollback()
        return LedgerSyncResult(None, len(rows), len(rows))
    if inserted < len(rows):
        await ledger_data.set_duplicate_entries(
            session, sync=sync, duplicate_entries=len(rows) - inserted
        )
    await session.commit()
    return LedgerSyncResult(sync, len(rows), len(rows) - inserted)


def _checked_rows(statement: _Statement) -> list[LedgerTransaction]:
    """CPU-bound (a hash per entry, the checks, the sort); runs in a worker thread.

    Raises LedgerResponseError.
    """
    entries = check_statement(statement.pages, reached_start=statement.reached_start)
    rows = [to_transaction(entry, statement.currency) for entry in entries]
    return ledger_data.in_insert_order(rows)


async def _read_statement(
    session: AsyncSession,
    ledger: LedgerSettings,
    token: str,
    *,
    owner_id: uuid.UUID,
    account_id: uuid.UUID,
    ledger_user_id: uuid.UUID,
    ledger_account_id: uuid.UUID,
) -> _Statement:
    # One deadline for the whole sync, applied to each ledger call only: cancelling a database
    # query halfway would leave its connection unusable, and a slow database is not the ledger.
    deadline = asyncio.get_running_loop().time() + ledger.sync_timeout_seconds
    async with ledger_client.LedgerClient.open(ledger.client, token, ledger.transport) as client:
        currency = await _before(
            deadline,
            client.account_currency(user_id=ledger_user_id, account_id=ledger_account_id),
        )
        pages: list[LedgerPage] = []
        seen: set[uuid.UUID] = set()
        known_entries = 0
        cursor = None
        while True:
            page = await _before(
                deadline,
                client.statement_page(
                    user_id=ledger_user_id, account_id=ledger_account_id, cursor=cursor
                ),
            )
            # Guards against a ledger that pages forever; check_statement checks the rest.
            page_ids = {entry.entry_id for entry in page.entries}
            if (page.next_cursor is not None and not page.entries) or page_ids & seen:
                raise LedgerResponseError("The ledger's pages do not advance.")
            seen |= page_ids
            pages.append(page)
            if len(seen) > ledger.max_entries:
                raise LedgerTooManyEntriesError
            known = await ledger_data.known_fingerprints(
                session,
                owner_id=owner_id,
                account_id=account_id,
                fingerprints=[ledger_fingerprint(entry.entry_id) for entry in page.entries],
            )
            await session.rollback()
            known_entries += len(known)
            if known or page.next_cursor is None:
                return _Statement(
                    currency,
                    pages,
                    reached_start=page.next_cursor is None,
                    known_entries=known_entries,
                )
            cursor = page.next_cursor


async def _before[T](deadline: float, call: Coroutine[Any, Any, T]) -> T:
    """Await a ledger call; past the sync's deadline it is cancelled and the ledger is
    unavailable."""
    try:
        async with asyncio.timeout_at(deadline):
            return await call
    except TimeoutError:
        raise ledger_client.LedgerUnavailableError("The ledger sync ran out of time.") from None


def _service_error(exc: ledger_client.LedgerError) -> Exception:
    if isinstance(exc, ledger_client.LedgerTokenRejectedError):
        return LedgerTokenRejectedError()
    if isinstance(exc, ledger_client.LedgerAccountUnavailableError):
        return LedgerAccountUnavailableError()
    if isinstance(exc, ledger_client.LedgerUnavailableError):
        return LedgerUnavailableError()
    return LedgerInvalidResponseError()


async def get_sync(session: AsyncSession, *, owner_id: uuid.UUID, sync_id: uuid.UUID) -> LedgerSync:
    sync = await ledger_data.get_sync(session, owner_id=owner_id, sync_id=sync_id)
    if sync is None:
        raise LedgerSyncNotFoundError
    return sync
