"""HTTP client for banking-ledger-api (ADR 0009). Turns its JSON into domain values; nothing else.

The ledger is another service, so its responses are untrusted input:
- The base URL comes from settings only. Paths are built from UUIDs this service stored, and the
  cursor goes in the query string as a UUID, so no request value can steer where a call goes.
  Redirects are not followed, and the environment (proxies, .netrc) is not consulted.
- Every response body is read as a stream and refused past a size limit, then validated strictly
  (an amount must be a JSON integer, a time must carry a time zone). Unknown fields are ignored,
  so the ledger can add fields without breaking this client.
- Bodies are never decompressed: the client asks for `identity` and refuses any other
  Content-Encoding, and counts the bytes as they arrive. A compressed body is only measurable
  after inflating it, and a few kilobytes of gzip can inflate to gigabytes.
- Errors carry fixed text only: never a status line, a body or a value the ledger sent. The token
  is sent in the Authorization header and appears in no error and no log line.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Self

import httpx2
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from transaction_analytics.domain.ledger import (
    LEDGER_MINOR_UNIT_EXPONENTS,
    LedgerEntry,
    LedgerEntryKind,
    LedgerPage,
)
from transaction_analytics.domain.money import Currency

# The ledger's maximum page size.
PAGE_SIZE = 100
# A full page is ~25 KB; anything far beyond that is not a statement page.
MAX_RESPONSE_BYTES = 1024 * 1024


class LedgerError(Exception):
    """A ledger call failed. Subclasses say how; the text is fixed."""


class LedgerTokenRejectedError(LedgerError):
    """The ledger answered 401: the token is unknown, expired or revoked."""


class LedgerAccountUnavailableError(LedgerError):
    """The ledger answered 403 or 404: the token's user cannot read this account."""


class LedgerUnavailableError(LedgerError):
    """The ledger could not be reached, timed out, or answered 429 or 5xx."""


class LedgerInvalidResponseError(LedgerError):
    """The ledger answered something that is not a valid response to the request."""


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class _AccountOut(_Strict):
    id: uuid.UUID
    currency: Currency


class _EntryOut(_Strict):
    entry_id: uuid.UUID
    kind: LedgerEntryKind
    amount_minor: int
    balance_after_minor: int
    created_at: AwareDatetime


class _StatementOut(_Strict):
    account_id: uuid.UUID
    balance_minor: int
    entries: list[_EntryOut]
    next_cursor: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    base_url: str
    # Per call: connecting, and waiting for each part of the response.
    timeout_seconds: float


class LedgerClient:
    """Calls for one sync, on one connection pool, with one user's token."""

    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        config: LedgerConfig,
        token: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> AsyncIterator[Self]:
        """`transport` replaces the network in tests (httpx2.MockTransport)."""
        async with httpx2.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # httpx2 asks for gzip, deflate and zstd by default (see _get).
                "Accept-Encoding": "identity",
            },
            timeout=httpx2.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            yield cls(client)

    async def account_currency(self, *, user_id: uuid.UUID, account_id: uuid.UUID) -> Currency:
        body = await self._get(f"/users/{user_id}/accounts/{account_id}", params={})
        account = _parse(_AccountOut, body)
        if account.id != account_id:
            raise LedgerInvalidResponseError("The ledger returned another account.")
        if account.currency not in LEDGER_MINOR_UNIT_EXPONENTS:
            raise LedgerInvalidResponseError("The ledger returned a currency it does not offer.")
        return account.currency

    async def statement_page(
        self, *, user_id: uuid.UUID, account_id: uuid.UUID, cursor: uuid.UUID | None
    ) -> LedgerPage:
        params = {"limit": str(PAGE_SIZE)}
        if cursor is not None:
            params["cursor"] = str(cursor)
        body = await self._get(f"/users/{user_id}/accounts/{account_id}/statement", params=params)
        statement = _parse(_StatementOut, body)
        if statement.account_id != account_id:
            raise LedgerInvalidResponseError("The ledger returned another account's statement.")
        if len(statement.entries) > PAGE_SIZE:
            raise LedgerInvalidResponseError("The ledger returned more entries than requested.")
        return LedgerPage(
            balance_minor=statement.balance_minor,
            entries=[
                LedgerEntry(
                    entry_id=entry.entry_id,
                    kind=entry.kind,
                    amount_minor=entry.amount_minor,
                    balance_after_minor=entry.balance_after_minor,
                    created_at=entry.created_at,
                )
                for entry in statement.entries
            ],
            next_cursor=statement.next_cursor,
        )

    async def _get(self, path: str, *, params: dict[str, str]) -> bytes:
        try:
            async with self._client.stream("GET", path, params=params) as response:
                _check_status(response.status_code)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise LedgerInvalidResponseError("The ledger's response is compressed.")
                body = bytearray()
                # Raw bytes: nothing is decoded, so what is counted is what is held.
                async for chunk in response.aiter_raw():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise LedgerInvalidResponseError("The ledger's response is too large.")
                return bytes(body)
        except httpx2.TransportError:
            # Connection refused, DNS failure, timeouts, a dropped connection: the ledger is not
            # available right now. The exception's text (host, address) is not passed on.
            raise LedgerUnavailableError("The ledger could not be reached.") from None


def _check_status(status: int) -> None:
    if status == 200:
        return
    if status == 401:
        raise LedgerTokenRejectedError("The ledger rejected the token.")
    if status in (403, 404):
        raise LedgerAccountUnavailableError("The ledger account is not available to this token.")
    if status == 429 or status >= 500:
        raise LedgerUnavailableError("The ledger is not available.")
    # Redirects and other client errors: this client sent only what the ledger documents, so
    # anything else means the two disagree.
    raise LedgerInvalidResponseError("The ledger answered with an unexpected status.")


def _parse[M: BaseModel](model: type[M], body: bytes) -> M:
    try:
        return model.model_validate_json(body)
    except ValidationError:
        # Pydantic's text quotes the offending input; only the fact is passed on.
        raise LedgerInvalidResponseError("The ledger's response is not valid.") from None
