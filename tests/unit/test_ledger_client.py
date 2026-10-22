"""The banking-ledger-api client against canned responses (httpx2.MockTransport, no network).

Every way the ledger can answer badly must end in one of four fixed errors, none of which carries
anything the ledger sent; the token goes only into the Authorization header.
"""

import gzip
import json
import logging
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest

from transaction_analytics.domain.ledger import LedgerEntryKind
from transaction_analytics.domain.money import Currency
from transaction_analytics.integrations.ledger import (
    MAX_RESPONSE_BYTES,
    PAGE_SIZE,
    LedgerAccountUnavailableError,
    LedgerClient,
    LedgerConfig,
    LedgerError,
    LedgerInvalidResponseError,
    LedgerTokenRejectedError,
    LedgerUnavailableError,
)

TOKEN = "lt_" + "T" * 43
USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
ACCOUNT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
ENTRY_ID = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
MARKER = "LEAK-ME-4410"
CONFIG = LedgerConfig(base_url="http://ledger.test/api", timeout_seconds=1.0)

Handler = Callable[[httpx2.Request], httpx2.Response]


def entry(**overrides: Any) -> dict[str, Any]:
    values = {
        "entry_id": str(ENTRY_ID),
        "transaction_id": str(uuid.uuid4()),
        "kind": "deposit",
        "amount_minor": 2500,
        "balance_after_minor": 2500,
        "created_at": "2026-09-01T12:00:00Z",
    }
    return values | overrides


def statement(**overrides: Any) -> dict[str, Any]:
    values = {
        "account_id": str(ACCOUNT_ID),
        "balance_minor": 2500,
        "entries": [entry()],
        "next_cursor": None,
    }
    return values | overrides


def streamed(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx2.Response:
    """A response that streams like one from the network.

    httpx2 reads (and decodes) a Response built from bytes or json= when it is constructed, so the
    client would never see it arrive; an async iterator is read only by the client.
    """

    async def chunks() -> AsyncIterator[bytes]:
        yield body

    return httpx2.Response(status, content=chunks(), headers=headers)


def streamed_json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx2.Response:
    return streamed(status, json.dumps(body).encode(), headers)


def hides_its_cause(exc: BaseException) -> bool:
    """No exception is shown with it: pydantic's and httpx2's texts quote input and hosts."""
    return exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


def answer(body: Any, status: int = 200) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed_json(status, body)

    return handler


async def read_page(handler: Handler, cursor: uuid.UUID | None = None) -> Any:
    async with LedgerClient.open(CONFIG, TOKEN, httpx2.MockTransport(handler)) as client:
        return await client.statement_page(user_id=USER_ID, account_id=ACCOUNT_ID, cursor=cursor)


async def read_currency(handler: Handler) -> Currency:
    async with LedgerClient.open(CONFIG, TOKEN, httpx2.MockTransport(handler)) as client:
        return await client.account_currency(user_id=USER_ID, account_id=ACCOUNT_ID)


async def test_statement_page_is_requested_and_parsed() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = statement(next_cursor=str(ENTRY_ID), extra_field="ignored")
        body["entries"][0]["new_ledger_field"] = 1  # the ledger may add fields
        return streamed_json(200, body)

    cursor = uuid.UUID(int=5)
    page = await read_page(handler, cursor=cursor)

    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == f"/api/users/{USER_ID}/accounts/{ACCOUNT_ID}/statement"
    assert dict(request.url.params) == {"limit": str(PAGE_SIZE), "cursor": str(cursor)}
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert page.balance_minor == 2500
    assert page.next_cursor == ENTRY_ID
    (parsed,) = page.entries
    assert parsed.entry_id == ENTRY_ID
    assert parsed.kind == LedgerEntryKind.DEPOSIT
    assert (parsed.amount_minor, parsed.balance_after_minor) == (2500, 2500)
    assert parsed.created_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def test_first_page_has_no_cursor_parameter() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return streamed_json(200, statement())

    await read_page(handler)

    assert dict(seen[0].url.params) == {"limit": str(PAGE_SIZE)}


async def test_account_currency_is_read() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return streamed_json(200, {"id": str(ACCOUNT_ID), "currency": "EUR", "balance_minor": 0})

    assert await read_currency(handler) == Currency.EUR
    assert seen[0].url.path == f"/api/users/{USER_ID}/accounts/{ACCOUNT_ID}"


@pytest.mark.parametrize(
    "body",
    [
        {"id": str(uuid.uuid4()), "currency": "EUR"},  # another account
        {"id": str(ACCOUNT_ID), "currency": "CHF"},  # a currency this service cannot store
        {"id": str(ACCOUNT_ID), "currency": "eur"},
        {"id": str(ACCOUNT_ID)},
    ],
)
async def test_invalid_account_is_rejected(body: dict[str, Any]) -> None:
    with pytest.raises(LedgerInvalidResponseError):
        await read_currency(answer(body))


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, LedgerTokenRejectedError),
        (403, LedgerAccountUnavailableError),
        (404, LedgerAccountUnavailableError),
        (429, LedgerUnavailableError),
        (500, LedgerUnavailableError),
        (502, LedgerUnavailableError),
        (503, LedgerUnavailableError),
        (301, LedgerInvalidResponseError),  # redirects are never followed
        (307, LedgerInvalidResponseError),
        (400, LedgerInvalidResponseError),
        (422, LedgerInvalidResponseError),  # e.g. a cursor the ledger does not know
        (201, LedgerInvalidResponseError),
    ],
)
async def test_status_is_mapped(status: int, error: type[LedgerError]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        headers = {"Location": "http://elsewhere.test/"} if 300 <= status < 400 else {}
        return streamed_json(status, {"code": MARKER, "detail": MARKER}, headers)

    with pytest.raises(error) as exc:
        await read_page(handler)

    assert MARKER not in str(exc.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError(MARKER),
        httpx2.ConnectTimeout(MARKER),
        httpx2.ReadTimeout(MARKER),
        httpx2.RemoteProtocolError(MARKER),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_transport_failure_is_unavailable(failure: Exception) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise failure

    with pytest.raises(LedgerUnavailableError) as exc:
        await read_page(handler)

    assert MARKER not in str(exc.value)
    assert hides_its_cause(exc.value)


def _entries(**overrides: Any) -> dict[str, Any]:
    return statement(entries=[entry(**overrides)])


@pytest.mark.parametrize(
    "body",
    [
        _entries(amount_minor="2500"),  # amounts are JSON integers only
        _entries(amount_minor=2500.0),
        _entries(amount_minor=True),
        _entries(amount_minor=None),
        _entries(balance_after_minor="2500"),
        _entries(kind="refund"),
        _entries(entry_id=MARKER),
        _entries(created_at="2026-09-01T12:00:00"),  # no time zone
        _entries(created_at=1756728000),
        _entries(created_at=MARKER),
        statement(account_id=str(uuid.uuid4())),  # another account's statement
        statement(entries=[entry(entry_id=str(uuid.uuid4())) for _ in range(PAGE_SIZE + 1)]),
        statement(next_cursor=MARKER),
        statement(balance_minor=None),
        {"entries": []},
        [],
        None,
    ],
    ids=lambda body: json.dumps(body)[:60],
)
async def test_malformed_statement_is_rejected(body: Any) -> None:
    with pytest.raises(LedgerInvalidResponseError) as exc:
        await read_page(answer(body))

    assert MARKER not in str(exc.value)
    assert hides_its_cause(exc.value)


@pytest.mark.parametrize(
    "content", [b"", b"not json", b'{"account_id": ', MARKER.encode(), b"\xff\xfe"]
)
async def test_non_json_body_is_rejected(content: bytes) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(200, content)

    with pytest.raises(LedgerInvalidResponseError) as exc:
        await read_page(handler)

    assert MARKER not in str(exc.value)


async def test_oversized_body_is_refused_while_streaming() -> None:
    chunks_sent = 0

    async def endless() -> Any:
        nonlocal chunks_sent
        for _ in range(100):
            chunks_sent += 1
            yield b" " * (64 * 1024)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=endless())

    with pytest.raises(LedgerInvalidResponseError):
        await read_page(handler)

    # Stopped right after the limit, not after reading everything.
    assert chunks_sent * 64 * 1024 <= MAX_RESPONSE_BYTES + 64 * 1024


async def test_token_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await read_page(answer(statement()))
        with pytest.raises(LedgerTokenRejectedError):
            await read_page(answer({}, status=401))

    assert TOKEN not in caplog.text
    assert "T" * 43 not in caplog.text


# Review fixes: compressed responses


def _gzipped(payload: bytes) -> Handler:
    body = gzip.compress(payload)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(200, body, headers={"Content-Encoding": "gzip"})

    return handler


async def test_uncompressed_responses_are_asked_for() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return streamed_json(200, statement())

    await read_page(handler)

    assert seen[0].headers["accept-encoding"] == "identity"


async def test_compressed_response_is_refused_even_when_valid() -> None:
    # A body is never decompressed: its size limit would only apply after inflating it.
    with pytest.raises(LedgerInvalidResponseError):
        await read_page(_gzipped(json.dumps(statement()).encode()))


async def test_broken_compressed_body_is_an_invalid_response() -> None:
    # Review fix: httpx2.DecodingError is not a TransportError and escaped as a 500.
    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(200, b"not gzip", headers={"Content-Encoding": "gzip"})

    with pytest.raises(LedgerInvalidResponseError):
        await read_page(handler)


async def test_compression_bomb_is_never_inflated() -> None:
    # 50 MB of zeros compress to ~50 KB. A guard, not a regression test: httpx2's decoder also
    # yields at most 1 MiB per chunk, so memory stayed bounded before the fix too. What the fix
    # adds is that the limit counts bytes on the wire (the tests above).
    handler = _gzipped(b"0" * (50 * 1024 * 1024))

    tracemalloc.start()
    try:
        with pytest.raises(LedgerInvalidResponseError):
            await read_page(handler)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 5 * MAX_RESPONSE_BYTES
