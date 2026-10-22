"""An in-memory banking-ledger-api for sync tests, served through httpx2.MockTransport.

Implements the two calls a sync makes, as the ledger documents them: GET an account (its
currency) and GET its statement (newest first, `limit`, `cursor` = an entry id, `next_cursor`,
the balance after each entry). One user, one account; postings are append-only, like the ledger.
Fault injection: answer every call with a status, or rewrite a statement body before it is sent.
"""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

TOKEN = "lt_" + "L" * 43
FIRST_POSTING = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

_ACCOUNT = re.compile(r"/users/([^/]+)/accounts/([^/]+)")
_STATEMENT = re.compile(r"/users/([^/]+)/accounts/([^/]+)/statement")


def _json(status: int, body: Any) -> httpx2.Response:
    # Streamed like a network response: httpx2 reads a Response built with json= at construction.
    content = json.dumps(body).encode()

    async def chunks() -> AsyncIterator[bytes]:
        yield content

    return httpx2.Response(status, content=chunks(), headers={"Content-Type": "application/json"})


@dataclass
class FakeLedger:
    user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    account_id: uuid.UUID = field(default_factory=uuid.uuid4)
    currency: str = "GBP"
    # The ledger's own maximum is 100; smaller pages exercise paging with few entries.
    max_page_size: int = 100
    # Set: every call is answered with this status (and an error body).
    fail_status: int | None = None
    # Set: applied to every statement body before it is sent.
    rewrite_statement: Callable[[dict[str, Any]], None] | None = None
    # Seconds to wait before answering, so concurrent syncs interleave.
    delay: float = 0.0
    entries: list[dict[str, Any]] = field(default_factory=list)  # oldest first
    requests: list[httpx2.Request] = field(default_factory=list)

    @property
    def balance(self) -> int:
        return self.entries[-1]["balance_after_minor"] if self.entries else 0

    def post(self, kind: str, amount_minor: int, at: datetime | None = None) -> uuid.UUID:
        entry_id = uuid.uuid4()
        self.entries.append(
            {
                "entry_id": str(entry_id),
                "transaction_id": str(uuid.uuid4()),
                "kind": kind,
                "amount_minor": amount_minor,
                "balance_after_minor": self.balance + amount_minor,
                "created_at": (at or FIRST_POSTING + timedelta(hours=len(self.entries)))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        return entry_id

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def statement_calls(self) -> int:
        return sum(1 for request in self.requests if request.url.path.endswith("/statement"))

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_status is not None:
            return _json(self.fail_status, {"code": "error", "detail": "fake"})
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _json(401, {"code": "invalid_token", "detail": "fake"})
        path = request.url.path
        match = _STATEMENT.fullmatch(path) or _ACCOUNT.fullmatch(path)
        if match is None:
            return _json(404, {"code": "not_found", "detail": "fake"})
        if match.group(1) != str(self.user_id):
            return _json(403, {"code": "forbidden", "detail": "fake"})
        if match.group(2) != str(self.account_id):
            return _json(404, {"code": "account_not_found", "detail": "fake"})
        if match.re is _ACCOUNT:
            body = {"id": str(self.account_id), "currency": self.currency, "balance_minor": 0}
            return _json(200, body)
        return _json(200, self._statement(request))

    def _statement(self, request: httpx2.Request) -> dict[str, Any]:
        limit = min(int(request.url.params.get("limit", "50")), self.max_page_size)
        newest_first = self.entries[::-1]
        start = 0
        cursor = request.url.params.get("cursor")
        if cursor is not None:
            start = 1 + next(i for i, e in enumerate(newest_first) if e["entry_id"] == cursor)
        page = newest_first[start : start + limit]
        more = start + limit < len(newest_first)
        body = {
            "account_id": str(self.account_id),
            "balance_minor": self.balance,
            "entries": [dict(entry) for entry in page],
            "next_cursor": page[-1]["entry_id"] if more else None,
        }
        if self.rewrite_statement is not None:
            self.rewrite_statement(body)
        return body
