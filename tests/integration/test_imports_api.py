"""Statement import end to end: HTTP -> service -> PostgreSQL (ADR 0004, ADR 0005)."""

import logging
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from factories import OTHER_OWNER_ID, OWNER_ID
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.api.deps import get_import_limits, get_owner_id
from transaction_analytics.data import imports as imports_data
from transaction_analytics.data.models import ImportJob, ImportRowError, Transaction
from transaction_analytics.domain.csv_parser import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS,
    ParsedTransaction,
    ParseResult,
)
from transaction_analytics.domain.imports import INTERNAL_ERROR_MESSAGE
from transaction_analytics.domain.money import Currency
from transaction_analytics.services import imports as import_service
from transaction_analytics.services.imports import ImportLimits

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"
VALID = (FIXTURES / "statement_valid.csv").read_bytes()
MIXED = (FIXTURES / "statement_mixed_errors.csv").read_bytes()
HEADER = b"date,description,amount,currency\n"


def act_as(app: FastAPI, owner_id: uuid.UUID) -> None:
    app.dependency_overrides[get_owner_id] = lambda: owner_id


def set_limits(app: FastAPI, *, max_bytes: int = DEFAULT_MAX_BYTES, max_rows: int = 3) -> None:
    limits = ImportLimits(max_bytes=max_bytes, max_rows=max_rows)
    app.dependency_overrides[get_import_limits] = lambda: limits


async def new_account(client: httpx2.AsyncClient, name: str = "Current account") -> str:
    response = await client.post("/accounts", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def upload(
    client: httpx2.AsyncClient,
    account_id: str,
    content: bytes | AsyncIterator[bytes],
    content_type: str | None = "text/csv",
) -> httpx2.Response:
    headers = {"Content-Type": content_type} if content_type else {}
    return await client.post(f"/accounts/{account_id}/imports", content=content, headers=headers)


async def count(session: AsyncSession, model: type, **filters: object) -> int:
    statement = select(func.count()).select_from(model).filter_by(**filters)
    return (await session.scalar(statement)) or 0


# --- happy paths ---------------------------------------------------------------------------------


async def test_valid_statement_is_imported(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    account_id = await new_account(client)

    response = await upload(client, account_id, VALID)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert (body["total_rows"], body["valid_rows"], body["invalid_rows"]) == (5, 5, 0)
    assert (body["duplicate_rows"], body["imported_rows"]) == (0, 5)
    assert body["error"] is None
    assert body["account_id"] == account_id
    assert body["started_at"] is not None
    assert body["finished_at"] is not None
    assert response.headers["Location"] == f"http://test/imports/{body['id']}"

    stored = list(
        await session.scalars(
            select(Transaction)
            .where(Transaction.import_job_id == uuid.UUID(body["id"]))
            .order_by(Transaction.row_number)
        )
    )
    assert [t.row_number for t in stored] == [2, 3, 4, 5, 6]
    assert [t.amount_minor for t in stored] == [250000, -320, -320, -4567, -12050]
    assert [t.currency for t in stored] == [Currency.GBP] * 4 + [Currency.EUR]
    assert stored[1].booked_on == date(2026, 9, 2)
    assert stored[1].description == "Coffee Shop"
    assert stored[1].normalized_description == "coffee shop"
    assert {str(t.account_id) for t in stored} == {account_id}
    assert await count(session, ImportRowError, import_job_id=uuid.UUID(body["id"])) == 0


async def test_import_reads_back_by_location(client: httpx2.AsyncClient) -> None:
    created = await upload(client, await new_account(client), VALID)

    response = await client.get(created.headers["Location"])

    assert response.status_code == 200
    assert response.json() == created.json()


async def test_location_includes_the_root_path_behind_a_proxy(app: FastAPI) -> None:
    # Mounted under a prefix (reverse proxy, root_path), the Location must point there too.
    transport = httpx2.ASGITransport(app=app, root_path="/api")
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await upload(client, await new_account(client), VALID)

    assert response.headers["Location"] == f"http://test/api/imports/{response.json()['id']}"


async def test_charset_parameter_is_accepted(client: httpx2.AsyncClient) -> None:
    response = await upload(client, await new_account(client), VALID, "text/csv; charset=utf-8")

    assert response.status_code == 201


async def test_mixed_statement_keeps_valid_rows_and_reports_errors(
    client: httpx2.AsyncClient,
) -> None:
    created = (await upload(client, await new_account(client), MIXED)).json()

    assert created["status"] == "completed"
    assert (created["total_rows"], created["valid_rows"], created["invalid_rows"]) == (8, 2, 6)

    response = await client.get(f"/imports/{created['id']}/errors")
    assert response.status_code == 200
    page = response.json()
    assert page["next_after_row"] is None
    assert [(e["row_number"], e["code"], e["column"]) for e in page["items"]] == [
        (3, "invalid_date", "date"),
        (4, "amount_too_precise", "amount"),
        (5, "invalid_amount_format", "amount"),
        (6, "unsupported_currency", "currency"),
        (7, "missing_value", "description"),
        (8, "column_count_mismatch", None),
    ]
    # Error messages never echo values from the file.
    for raw_value in ("2026-09-31", "-1.005", "1,200.00", "XYZ", "Short row", "Unknown currency"):
        assert raw_value not in response.text


async def test_invariants_counts_match_rows_and_rows_are_disjoint(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    body = (await upload(client, await new_account(client), MIXED)).json()
    job_id = uuid.UUID(body["id"])

    transaction_rows = set(
        await session.scalars(
            select(Transaction.row_number).where(Transaction.import_job_id == job_id)
        )
    )
    error_rows = set(
        await session.scalars(
            select(ImportRowError.row_number).where(ImportRowError.import_job_id == job_id)
        )
    )

    assert len(transaction_rows) == body["imported_rows"] == body["valid_rows"]
    assert body["duplicate_rows"] == 0
    assert len(error_rows) == body["invalid_rows"]
    assert transaction_rows.isdisjoint(error_rows)
    assert transaction_rows | error_rows == set(range(2, 2 + body["total_rows"]))


async def test_file_with_only_invalid_rows_completes_with_zero_valid_rows(
    client: httpx2.AsyncClient,
) -> None:
    # No threshold (ADR 0004): every row error is reported instead of one generic failure.
    content = HEADER + b"2026-02-30,a,1.00,GBP\n2026-09-01,b,1.00,XYZ\n"

    body = (await upload(client, await new_account(client), content)).json()

    assert body["status"] == "completed"
    assert (body["valid_rows"], body["invalid_rows"]) == (0, 2)


# --- rejected files: a failed job, no rows ------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "empty_file"),
        (b"\xff\xfed\x00a\x00t\x00e\x00", "invalid_encoding"),
        (b"date,description,amount\n2026-09-01,x,1.00\n", "missing_columns"),
        (b"date,Date,description,amount,currency\n", "duplicate_columns"),
        (HEADER, "no_data_rows"),
        (HEADER + b'2026-09-01,"unterminated,1.00,GBP\n', "malformed_csv"),
        (HEADER + b"2026-09-01,a,1.00,GBP\n" * 4, "too_many_rows"),
    ],
)
async def test_rejected_file_creates_a_failed_import_without_rows(
    app: FastAPI, client: httpx2.AsyncClient, session: AsyncSession, content: bytes, code: str
) -> None:
    set_limits(app, max_rows=3)

    response = await upload(client, await new_account(client), content)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert (body["total_rows"], body["valid_rows"], body["invalid_rows"]) == (0, 0, 0)
    assert (body["duplicate_rows"], body["imported_rows"]) == (0, 0)
    assert body["finished_at"] is not None
    assert await count(session, Transaction) == 0
    assert await count(session, ImportRowError) == 0


async def test_exactly_max_rows_is_accepted(app: FastAPI, client: httpx2.AsyncClient) -> None:
    set_limits(app, max_rows=3)

    response = await upload(client, await new_account(client), HEADER + b"2026-09-01,a,1,GBP\n" * 3)

    assert response.json()["status"] == "completed"
    assert response.json()["valid_rows"] == 3


async def test_default_row_limit_is_importable(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    # Boundary of the default limits and a rough timing check (see `pytest --durations`).
    rows = b"".join(b"2026-09-01,Row %d,-1.00,GBP\n" % index for index in range(DEFAULT_MAX_ROWS))
    content = HEADER + rows
    assert len(content) <= DEFAULT_MAX_BYTES

    response = await upload(client, await new_account(client), content)

    assert response.json()["valid_rows"] == DEFAULT_MAX_ROWS
    assert await count(session, Transaction) == DEFAULT_MAX_ROWS


# --- refused requests: no job at all ------------------------------------------------------------


async def test_body_over_the_limit_is_refused(
    app: FastAPI, client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    account_id = await new_account(client)
    set_limits(app, max_bytes=len(VALID))

    exact = await upload(client, account_id, VALID)
    too_large = await upload(client, account_id, VALID + b"\n")

    assert exact.status_code == 201
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "payload_too_large"
    assert await count(session, ImportJob) == 1


async def test_chunked_body_over_the_limit_is_refused(
    app: FastAPI, client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    account_id = await new_account(client)
    set_limits(app, max_bytes=100)

    async def chunks() -> AsyncIterator[bytes]:
        # No Content-Length: only counting the stream can catch this.
        for _ in range(10):
            yield HEADER

    response = await upload(client, account_id, chunks())

    assert response.status_code == 413
    assert await count(session, ImportJob) == 0


@pytest.mark.parametrize("content_type", ["application/json", "multipart/form-data", None])
async def test_wrong_content_type_is_refused(
    client: httpx2.AsyncClient, session: AsyncSession, content_type: str | None
) -> None:
    response = await upload(client, await new_account(client), VALID, content_type)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert await count(session, ImportJob) == 0


# --- duplicates and re-upload --------------------------------------------------------------------


async def test_same_file_twice_is_a_conflict(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    account_id = await new_account(client)
    first = (await upload(client, account_id, VALID)).json()

    second = await upload(client, account_id, VALID)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_import"
    assert second.json()["error"]["import_id"] == first["id"]
    assert await count(session, ImportJob) == 1
    assert await count(session, Transaction) == first["valid_rows"]


async def test_overlapping_statement_reports_its_duplicates(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    # ADR 0006: rows the account already holds are skipped and counted, not stored again.
    account_id = await new_account(client)
    await upload(client, account_id, HEADER + b"2026-09-01,Rent,-950.00,GBP\n")
    overlapping = HEADER + b"2026-09-01,Rent,-950.00,GBP\n2026-09-02,Books,-12.00,GBP\n"

    response = await upload(client, account_id, overlapping)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert (body["valid_rows"], body["duplicate_rows"], body["imported_rows"]) == (2, 1, 1)
    assert (await client.get(f"/imports/{body['id']}")).json() == body
    assert await count(session, Transaction, import_job_id=uuid.UUID(body["id"])) == 1
    assert await count(session, Transaction) == 2


async def test_same_statement_with_a_bom_imports_nothing_new(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    # Different bytes, so no file-level 409; every row is a duplicate instead.
    account_id = await new_account(client)
    await upload(client, account_id, VALID)

    response = await upload(client, account_id, b"\xef\xbb\xbf" + VALID)

    assert response.status_code == 201
    body = response.json()
    assert (body["valid_rows"], body["duplicate_rows"], body["imported_rows"]) == (5, 5, 0)
    assert await count(session, Transaction) == 5


async def test_default_row_limit_is_re_importable(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    # Every row conflicts on the dedup constraint: the worst case for ON CONFLICT DO NOTHING.
    # Rough timing in `pytest --durations` (ADR 0006).
    rows = b"".join(b"2026-09-01,Row %d,-1.00,GBP\n" % index for index in range(DEFAULT_MAX_ROWS))
    account_id = await new_account(client)
    await upload(client, account_id, HEADER + rows)

    response = await upload(client, account_id, b"\xef\xbb\xbf" + HEADER + rows)

    body = response.json()
    assert (body["duplicate_rows"], body["imported_rows"]) == (DEFAULT_MAX_ROWS, 0)
    assert await count(session, Transaction) == DEFAULT_MAX_ROWS


async def test_same_file_into_another_account_is_allowed(client: httpx2.AsyncClient) -> None:
    await upload(client, await new_account(client, "Current"), VALID)

    response = await upload(client, await new_account(client, "Savings"), VALID)

    assert response.status_code == 201


async def test_retry_after_an_internal_error_creates_a_new_import(
    client: httpx2.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = await new_account(client)
    monkeypatch.setattr(import_service, "parse_statement_csv", broken_parser)
    failed_id = (await upload(client, account_id, VALID)).json()["error"]["import_id"]
    monkeypatch.undo()

    response = await upload(client, account_id, VALID)

    assert response.status_code == 201
    assert response.json()["status"] == "completed"
    assert response.json()["id"] != failed_id
    assert await count(session, ImportJob) == 2  # the failed attempt stays as history


async def test_same_rejected_file_again_returns_the_existing_failed_import(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    # A rejection is deterministic: the same bytes fail the same way. Re-sending them must not
    # add one failed job per attempt (a retry loop would grow the table without bound).
    account_id = await new_account(client)
    first = await upload(client, account_id, b"")

    again = await upload(client, account_id, b"")

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()
    assert again.headers["Content-Location"] == first.headers["Location"]
    assert await count(session, ImportJob) == 1


async def test_rejected_file_is_imported_when_the_outcome_changes(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    account_id = await new_account(client)
    content = HEADER + b"2026-09-01,a,1.00,GBP\n" * 4
    set_limits(app, max_rows=3)
    rejected = (await upload(client, account_id, content)).json()
    set_limits(app, max_rows=10)

    response = await upload(client, account_id, content)

    assert rejected["error"]["code"] == "too_many_rows"
    assert response.status_code == 201
    assert response.json()["status"] == "completed"


# --- ownership -----------------------------------------------------------------------------------


async def test_another_owner_cannot_import_into_or_read_an_account(
    app: FastAPI, client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    account_id = await new_account(client)
    import_id = (await upload(client, account_id, MIXED)).json()["id"]

    act_as(app, OTHER_OWNER_ID)
    upload_response = await upload(client, account_id, VALID)
    read_response = await client.get(f"/imports/{import_id}")
    errors_response = await client.get(f"/imports/{import_id}/errors")

    assert upload_response.status_code == 404
    assert upload_response.json()["error"]["code"] == "account_not_found"
    assert read_response.status_code == 404
    assert read_response.json()["error"]["code"] == "import_not_found"
    assert errors_response.status_code == 404
    assert await count(session, ImportJob) == 1


async def test_unknown_and_malformed_ids(client: httpx2.AsyncClient) -> None:
    assert (await upload(client, str(uuid.uuid4()), VALID)).status_code == 404
    assert (await client.get(f"/imports/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(f"/imports/{uuid.uuid4()}/errors")).status_code == 404
    malformed = await upload(client, "not-a-uuid", VALID)
    assert malformed.status_code == 422
    assert "not-a-uuid" not in malformed.text


# --- unexpected database errors ------------------------------------------------------------------

MARKER = "LEAK-ME-5678"
OUT_OF_RANGE_AMOUNT = 2**63  # one past BIGINT; asyncpg rejects it client-side with the value


def broken_parser(content: bytes, **limits: int) -> ParseResult:
    """A parser bug that lets an out-of-range amount through: the realistic leak case from
    test_error_leaks.py, where even str() of the database exception contains the value."""
    row = ParsedTransaction(
        row_number=2,
        booked_on=date(2026, 9, 1),
        description=f"Payment {MARKER}",
        normalized_description=f"payment {MARKER.casefold()}",
        amount_minor=OUT_OF_RANGE_AMOUNT,
        currency=Currency.GBP,
    )
    return ParseResult(transactions=[row])


async def test_database_error_records_a_failed_import_and_leaks_nothing(
    client: httpx2.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(import_service, "parse_statement_csv", broken_parser)
    account_id = await new_account(client)

    with caplog.at_level(logging.DEBUG):
        response = await upload(client, account_id, VALID)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    failed = (await client.get(f"/imports/{error['import_id']}")).json()
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "internal_error"
    assert await count(session, Transaction) == 0

    assert "DataError" in caplog.text
    for secret in (MARKER, MARKER.casefold(), str(OUT_OF_RANGE_AMOUNT)):
        assert secret not in caplog.text
        assert secret not in response.text


async def test_no_transaction_is_open_while_parsing(
    client: httpx2.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Parsing is CPU-bound and slow for big files; holding a connection "idle in transaction"
    # meanwhile would let a few concurrent uploads drain the pool.
    in_transaction: list[bool] = []
    real_parser = import_service.parse_statement_csv

    def recording_parser(content: bytes, **limits: int) -> ParseResult:
        in_transaction.append(session.in_transaction())
        return real_parser(content, **limits)

    monkeypatch.setattr(import_service, "parse_statement_csv", recording_parser)

    response = await upload(client, await new_account(client), VALID)

    assert response.status_code == 201
    assert in_transaction == [False]


async def test_file_is_hashed_off_the_event_loop(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_event_loop_thread: list[bool] = []
    real_sha256 = import_service.hashlib.sha256

    def recording_sha256(data: bytes) -> object:
        on_event_loop_thread.append(threading.current_thread() is threading.main_thread())
        return real_sha256(data)

    # Replaces the service's hashlib only, not the module everybody shares.
    monkeypatch.setattr(import_service, "hashlib", SimpleNamespace(sha256=recording_sha256))

    response = await upload(client, await new_account(client), VALID)

    assert response.status_code == 201
    assert on_event_loop_thread == [False]


async def test_failure_to_record_the_failed_import_still_returns_500(
    client: httpx2.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(import_service, "parse_statement_csv", broken_parser)
    real_insert_job = imports_data.insert_job
    calls = 0

    async def insert_job_failing_the_second_time(session: AsyncSession, **values: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:  # the attempt to record internal_error
            raise DBAPIError("INSERT INTO import_jobs ...", None, Exception(f"boom {MARKER}"))
        return await real_insert_job(session, **values)

    monkeypatch.setattr(imports_data, "insert_job", insert_job_failing_the_second_time)
    account_id = await new_account(client)

    with caplog.at_level(logging.DEBUG):
        response = await upload(client, account_id, VALID)

    assert calls == 2
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": INTERNAL_ERROR_MESSAGE}
    }
    assert await count(session, ImportJob) == 0
    assert "Recording a failed import failed too" in caplog.text
    assert MARKER not in caplog.text


async def test_row_error_query_is_owner_scoped_on_its_own(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    # Through the API the job's owner is checked first, which would hide a missing filter here.
    job_id = uuid.UUID((await upload(client, await new_account(client), MIXED)).json()["id"])

    def page(owner_id: uuid.UUID) -> object:
        return imports_data.list_row_errors(
            session, owner_id=owner_id, import_job_id=job_id, after_row=1, limit=100
        )

    assert len(await page(OWNER_ID)) == 6
    assert await page(OTHER_OWNER_ID) == []


# --- row error pagination ------------------------------------------------------------------------


@pytest.fixture
async def five_errors(client: httpx2.AsyncClient) -> str:
    content = HEADER + b"".join(b"2026-02-%d,x,1.00,GBP\n" % day for day in range(30, 35))
    body = (await upload(client, await new_account(client), content)).json()
    assert body["invalid_rows"] == 5  # rows 2..6
    return body["id"]


async def test_errors_are_paged_by_row_number(client: httpx2.AsyncClient, five_errors: str) -> None:
    url = f"/imports/{five_errors}/errors"

    pages = []
    after_row = 1
    while after_row is not None:
        page = (await client.get(url, params={"after_row": after_row, "limit": 2})).json()
        pages.append([item["row_number"] for item in page["items"]])
        after_row = page["next_after_row"]

    assert pages == [[2, 3], [4, 5], [6]]


async def test_page_of_exactly_the_remaining_rows_is_the_last(
    client: httpx2.AsyncClient, five_errors: str
) -> None:
    page = (await client.get(f"/imports/{five_errors}/errors", params={"limit": 5})).json()

    assert len(page["items"]) == 5
    assert page["next_after_row"] is None


async def test_page_after_the_last_row_is_empty(
    client: httpx2.AsyncClient, five_errors: str
) -> None:
    page = (await client.get(f"/imports/{five_errors}/errors", params={"after_row": 100})).json()

    assert page == {"items": [], "next_after_row": None}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"after_row": 0}])
async def test_invalid_paging_parameters(
    client: httpx2.AsyncClient, five_errors: str, params: dict
) -> None:
    response = await client.get(f"/imports/{five_errors}/errors", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
