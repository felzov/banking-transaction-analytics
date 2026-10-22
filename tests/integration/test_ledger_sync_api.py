"""Ledger accounts and syncs end to end: HTTP -> service -> fake ledger + PostgreSQL (ADR 0009)."""

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import httpx2
import pytest
from factories import OTHER_OWNER_ID, OWNER_ID
from fake_ledger import TOKEN, FakeLedger
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.api.deps import get_ledger_settings, get_owner_id
from transaction_analytics.data import accounts as accounts_data
from transaction_analytics.data import ledger as ledger_data
from transaction_analytics.data.models import ImportJob, LedgerSync, Transaction
from transaction_analytics.domain.ledger import AccountSource, LedgerLink
from transaction_analytics.integrations.ledger import LedgerConfig
from transaction_analytics.services import ledger_sync as ledger_sync_service
from transaction_analytics.services.ledger_sync import LedgerSettings

pytestmark = pytest.mark.integration

CSV = b"date,description,amount,currency\n2026-09-01,Coffee,-3.20,GBP\n"


@pytest.fixture
def ledger() -> FakeLedger:
    return FakeLedger()


def use_ledger(
    app: FastAPI, ledger: FakeLedger, *, max_entries: int = 50_000, sync_timeout: float = 10.0
) -> None:
    settings = LedgerSettings(
        client=LedgerConfig(base_url="http://ledger.test", timeout_seconds=5.0),
        sync_timeout_seconds=sync_timeout,
        max_entries=max_entries,
        transport=ledger.transport(),
    )
    app.dependency_overrides[get_ledger_settings] = lambda: settings


@pytest.fixture(autouse=True)
def _ledger_settings(app: FastAPI, ledger: FakeLedger) -> None:
    use_ledger(app, ledger)


def act_as(app: FastAPI, owner_id: uuid.UUID) -> None:
    app.dependency_overrides[get_owner_id] = lambda: owner_id


async def link(client: httpx2.AsyncClient, ledger: FakeLedger) -> dict:
    response = await client.post(
        "/accounts",
        json={
            "name": "Ledger account",
            "ledger": {"user_id": str(ledger.user_id), "account_id": str(ledger.account_id)},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def sync(client: httpx2.AsyncClient, account_id: str, token: str = TOKEN) -> httpx2.Response:
    return await client.post(f"/accounts/{account_id}/ledger-syncs", json={"ledger_token": token})


async def synced(client: httpx2.AsyncClient, account_id: str) -> dict:
    response = await sync(client, account_id)
    assert response.status_code == 201, response.text
    return response.json()


async def listed(client: httpx2.AsyncClient, account_id: str) -> list[dict]:
    response = await client.get(f"/accounts/{account_id}/transactions")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def stored(session: AsyncSession, account_id: str) -> tuple[int, int]:
    """(syncs, transactions) stored for the account."""
    account = uuid.UUID(account_id)
    syncs = await session.scalar(
        select(func.count()).select_from(LedgerSync).where(LedgerSync.account_id == account)
    )
    transactions = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account)
    )
    return syncs or 0, transactions or 0


def post_history(ledger: FakeLedger) -> None:
    ledger.post("deposit", 250_000)
    ledger.post("withdrawal", -4_000)
    ledger.post("transfer", -12_050)
    ledger.post("transfer", 1_500)
    ledger.post("withdrawal", -320)


# Linking


async def test_ledger_account_is_created_with_its_link(
    client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    account = await link(client, ledger)

    assert account["source"] == "ledger"
    assert account["ledger"] == {
        "user_id": str(ledger.user_id),
        "account_id": str(ledger.account_id),
    }
    assert (await client.get(f"/accounts/{account['id']}")).json() == account
    # Nothing is asked of the ledger until a sync brings a token.
    assert ledger.requests == []


async def test_csv_account_has_no_link(client: httpx2.AsyncClient) -> None:
    response = await client.post("/accounts", json={"name": "Current account"})

    assert (response.json()["source"], response.json()["ledger"]) == ("csv", None)


async def test_one_owner_links_a_ledger_account_once(
    app: FastAPI, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    await link(client, ledger)

    again = await client.post(
        "/accounts",
        json={
            "name": "Again",
            "ledger": {"user_id": str(ledger.user_id), "account_id": str(ledger.account_id)},
        },
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ledger_account_already_linked"
    act_as(app, OTHER_OWNER_ID)  # another owner may link it (the ledger decides who reads it)
    await link(client, ledger)


async def test_data_layer_takes_the_link_as_one_value(session: AsyncSession) -> None:
    # Review fix: two separate optional ids let a caller pass half a link, which only the
    # database refused (an unmapped IntegrityError, i.e. a 500).
    link_ = LedgerLink(user_id=uuid.uuid4(), account_id=uuid.uuid4())

    ledger_acc = await accounts_data.add_account(
        session, owner_id=OWNER_ID, name="Ledger", ledger=link_
    )
    csv_acc = await accounts_data.add_account(session, owner_id=OWNER_ID, name="Current")

    assert (ledger_acc.source, ledger_acc.ledger_user_id, ledger_acc.ledger_account_id) == (
        AccountSource.LEDGER,
        link_.user_id,
        link_.account_id,
    )
    assert (csv_acc.source, csv_acc.ledger_user_id, csv_acc.ledger_account_id) == (
        AccountSource.CSV,
        None,
        None,
    )


# Syncing


async def test_first_sync_stores_the_whole_history(
    client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    post_history(ledger)
    account = await link(client, ledger)

    response = await sync(client, account["id"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["fetched_entries"], body["duplicate_entries"], body["imported_entries"]) == (
        5,
        0,
        5,
    )
    assert body["account_id"] == account["id"]
    location = response.headers["location"]
    assert location == f"http://test/ledger-syncs/{body['id']}"
    assert (await client.get(location)).json() == body

    items = await listed(client, account["id"])
    # All five are booked on one day, where the listing's order is by random id (ADR 0008).
    by_entry = {item["ledger_entry_id"]: item for item in items}
    assert set(by_entry) == {entry["entry_id"] for entry in ledger.entries}
    last = by_entry[ledger.entries[-1]["entry_id"]]
    assert (last["source"], last["import_id"]) == ("ledger", None)
    assert (last["booked_on"], last["description"], last["amount"], last["currency"]) == (
        "2026-09-01",
        "Ledger withdrawal",
        "-3.20",
        "GBP",
    )
    assert {item["description"] for item in items} == {
        "Ledger deposit",
        "Ledger withdrawal",
        "Ledger transfer in",
        "Ledger transfer out",
    }


async def test_summary_of_a_synced_account_matches_the_ledger_balance(
    client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    post_history(ledger)
    account = await link(client, ledger)
    await synced(client, account["id"])

    (totals,) = (await client.get(f"/accounts/{account['id']}/summary")).json()["totals"]

    assert totals["net_minor"] == ledger.balance
    assert (totals["inflow_minor"], totals["outflow_minor"]) == (251_500, -16_370)
    assert totals["transactions"] == 5


async def test_history_is_read_across_pages(client: httpx2.AsyncClient, ledger: FakeLedger) -> None:
    ledger.max_page_size = 2
    post_history(ledger)  # 5 entries: pages of 2, 2, 1
    account = await link(client, ledger)

    body = await synced(client, account["id"])

    assert body["imported_entries"] == 5
    assert ledger.statement_calls() == 3


async def test_sync_without_changes_reads_one_page_and_adds_nothing(
    client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    ledger.max_page_size = 2
    post_history(ledger)
    account = await link(client, ledger)
    await synced(client, account["id"])
    calls = ledger.statement_calls()

    response = await sync(client, account["id"])

    # The first page already holds stored entries: everything older is stored too.
    assert ledger.statement_calls() == calls + 1
    assert response.status_code == 204
    assert len(await listed(client, account["id"])) == 5


async def test_sync_after_new_postings_adds_only_them(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    ledger.max_page_size = 2
    post_history(ledger)
    account = await link(client, ledger)
    await synced(client, account["id"])
    new = [ledger.post("deposit", 700), ledger.post("deposit", 800), ledger.post("deposit", 900)]

    body = await synced(client, account["id"])

    # Pages [900, 800], [700, old]: stops at the page with a stored entry.
    assert (body["fetched_entries"], body["duplicate_entries"], body["imported_entries"]) == (
        4,
        1,
        3,
    )
    items = await listed(client, account["id"])
    assert len(items) == 8
    assert {str(entry_id) for entry_id in new} <= {item["ledger_entry_id"] for item in items}
    assert await stored(session, account["id"]) == (2, 8)
    (totals,) = (await client.get(f"/accounts/{account['id']}/summary")).json()["totals"]
    assert totals["net_minor"] == ledger.balance


async def test_empty_ledger_account_syncs_to_nothing(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    account = await link(client, ledger)

    response = await sync(client, account["id"])

    assert response.status_code == 204
    assert await stored(session, account["id"]) == (0, 0)


async def test_entries_are_booked_on_their_utc_date(
    client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    # 23:30 at UTC-1 on 30 September is 00:30 UTC on 1 October.
    late = datetime(2026, 9, 30, 23, 30, tzinfo=timezone(timedelta(hours=-1)))
    ledger.post("deposit", 1000, at=datetime(2026, 9, 15, tzinfo=UTC))
    ledger.post("deposit", 500, at=late.astimezone(UTC))
    account = await link(client, ledger)
    await synced(client, account["id"])

    months = (await client.get(f"/accounts/{account['id']}/summary/monthly")).json()["months"]

    assert [(month["month"], month["net_minor"]) for month in months] == [
        ("2026-09", 1000),
        ("2026-10", 500),
    ]


# Failures: nothing is stored


@pytest.mark.parametrize(
    ("setup", "status", "code"),
    [
        (lambda ledger: setattr(ledger, "fail_status", 401), 422, "ledger_token_rejected"),
        (lambda ledger: setattr(ledger, "fail_status", 403), 422, "ledger_account_unavailable"),
        (lambda ledger: setattr(ledger, "fail_status", 404), 422, "ledger_account_unavailable"),
        (lambda ledger: setattr(ledger, "fail_status", 503), 502, "ledger_unavailable"),
        (lambda ledger: setattr(ledger, "currency", "CHF"), 502, "ledger_invalid_response"),
    ],
    ids=["401", "403", "404", "503", "unsupported-currency"],
)
async def test_ledger_failure_stores_nothing(
    session: AsyncSession,
    client: httpx2.AsyncClient,
    ledger: FakeLedger,
    setup: object,
    status: int,
    code: str,
) -> None:
    post_history(ledger)
    account = await link(client, ledger)
    setup(ledger)  # type: ignore[operator]

    response = await sync(client, account["id"])

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert await stored(session, account["id"]) == (0, 0)


async def test_wrong_ledger_token_is_rejected(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    post_history(ledger)
    account = await link(client, ledger)

    response = await sync(client, account["id"], token="lt_" + "X" * 43)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ledger_token_rejected"
    assert await stored(session, account["id"]) == (0, 0)


async def test_link_to_another_ledger_user_is_unavailable(
    client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    response = await client.post(
        "/accounts",
        json={
            "name": "Not mine",
            "ledger": {"user_id": str(uuid.uuid4()), "account_id": str(ledger.account_id)},
        },
    )

    synced_response = await sync(client, response.json()["id"])

    assert synced_response.status_code == 422
    assert synced_response.json()["error"]["code"] == "ledger_account_unavailable"


def _break_chain(body: dict) -> None:
    if body["entries"]:
        body["entries"][-1]["balance_after_minor"] += 1


def _drop_an_entry(body: dict) -> None:
    # A page that silently loses an entry: the chain no longer adds up.
    if len(body["entries"]) > 2:
        del body["entries"][1]


@pytest.mark.parametrize(
    "rewrite",
    [_break_chain, _drop_an_entry],
    ids=["broken-chain", "dropped-entry"],
)
async def test_inconsistent_statement_stores_nothing(
    session: AsyncSession,
    client: httpx2.AsyncClient,
    ledger: FakeLedger,
    rewrite: object,
) -> None:
    post_history(ledger)
    account = await link(client, ledger)
    ledger.rewrite_statement = rewrite  # type: ignore[assignment]

    response = await sync(client, account["id"])

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ledger_invalid_response"
    assert await stored(session, account["id"]) == (0, 0)


async def test_cut_off_history_is_rejected(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    # The ledger says there is nothing older, but the oldest entry does not start at zero.
    ledger.max_page_size = 2
    post_history(ledger)
    account = await link(client, ledger)
    ledger.rewrite_statement = lambda body: body.update(next_cursor=None)

    response = await sync(client, account["id"])

    assert response.status_code == 502
    assert await stored(session, account["id"]) == (0, 0)


async def test_history_longer_than_the_limit_is_refused(
    app: FastAPI, session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    ledger.max_page_size = 2
    post_history(ledger)
    use_ledger(app, ledger, max_entries=3)
    account = await link(client, ledger)

    response = await sync(client, account["id"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ledger_too_many_entries"
    assert await stored(session, account["id"]) == (0, 0)


# Ownership and sources


async def test_another_owners_account_is_not_found_and_the_ledger_is_not_called(
    app: FastAPI, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    account = await link(client, ledger)
    act_as(app, OTHER_OWNER_ID)

    response = await sync(client, account["id"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "account_not_found"
    assert ledger.requests == []


async def test_csv_account_cannot_be_synced(client: httpx2.AsyncClient, ledger: FakeLedger) -> None:
    account = (await client.post("/accounts", json={"name": "Current"})).json()

    response = await sync(client, account["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "account_source_mismatch"
    assert ledger.requests == []


async def test_ledger_account_takes_no_csv_upload(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    account = await link(client, ledger)

    response = await client.post(
        f"/accounts/{account['id']}/imports", content=CSV, headers={"Content-Type": "text/csv"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "account_source_mismatch"
    jobs = await session.scalar(
        select(func.count())
        .select_from(ImportJob)
        .where(ImportJob.account_id == uuid.UUID(account["id"]))
    )
    assert jobs == 0


async def test_another_owners_sync_is_not_found(
    app: FastAPI, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    post_history(ledger)
    account = await link(client, ledger)
    body = await synced(client, account["id"])
    act_as(app, OTHER_OWNER_ID)

    response = await client.get(f"/ledger-syncs/{body['id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ledger_sync_not_found"
    act_as(app, OWNER_ID)
    assert (await client.get(f"/ledger-syncs/{body['id']}")).status_code == 200


# The token


async def test_token_is_not_stored_logged_or_returned(
    session: AsyncSession,
    client: httpx2.AsyncClient,
    ledger: FakeLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    post_history(ledger)
    account = await link(client, ledger)

    with caplog.at_level(logging.DEBUG):
        ok = await sync(client, account["id"])
        ledger.fail_status = 503
        failed = await sync(client, account["id"])

    assert (ok.status_code, failed.status_code) == (201, 502)
    secret = TOKEN.removeprefix("lt_")
    assert secret not in ok.text + failed.text
    assert secret not in caplog.text
    rows = await session.scalars(
        text(
            "SELECT row_to_json(t)::text FROM ledger_syncs t"
            " UNION ALL SELECT row_to_json(t)::text FROM transactions t"
            " UNION ALL SELECT row_to_json(t)::text FROM accounts t"
        )
    )
    assert not any(secret in row for row in rows)


# The sync deadline (review fix: it covered the database work between ledger calls too)


async def test_sync_deadline_stops_a_slow_ledger(
    app: FastAPI, session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    post_history(ledger)
    account = await link(client, ledger)
    ledger.delay = 0.3
    use_ledger(app, ledger, sync_timeout=0.2)

    response = await sync(client, account["id"])

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ledger_unavailable"
    assert await stored(session, account["id"]) == (0, 0)


async def test_sync_deadline_does_not_cut_database_work(
    app: FastAPI,
    client: httpx2.AsyncClient,
    ledger: FakeLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cancelling an asyncpg query halfway leaves its connection unusable; only ledger calls are
    # bounded by the deadline.
    original = ledger_data.known_fingerprints

    async def slow_lookup(*args: object, **kwargs: object) -> set[bytes]:
        await asyncio.sleep(0.3)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    post_history(ledger)
    account = await link(client, ledger)
    use_ledger(app, ledger, sync_timeout=0.2)
    monkeypatch.setattr(ledger_data, "known_fingerprints", slow_lookup)

    response = await sync(client, account["id"])

    assert response.status_code == 201, response.text
    assert response.json()["imported_entries"] == 5


async def test_database_timeout_is_not_blamed_on_the_ledger(
    client: httpx2.AsyncClient, ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timing_out(*args: object, **kwargs: object) -> set[bytes]:
        raise TimeoutError

    post_history(ledger)
    account = await link(client, ledger)
    monkeypatch.setattr(ledger_data, "known_fingerprints", timing_out)

    response = await sync(client, account["id"])

    # Not 502 ledger_unavailable: the ledger answered; our own side failed.
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# Review fixes (round 2)


async def test_statement_is_checked_and_mapped_off_the_event_loop(
    client: httpx2.AsyncClient, ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Checking, mapping and sorting 50 000 entries blocked the loop for ~0.26 s (measured); like
    # the CSV parse (ADR 0004), they run in a worker thread.
    threads: set[int] = set()

    def recording[**P, R](function: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            threads.add(threading.get_ident())
            return function(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        ledger_sync_service, "check_statement", recording(ledger_sync_service.check_statement)
    )
    monkeypatch.setattr(
        ledger_sync_service, "to_transaction", recording(ledger_sync_service.to_transaction)
    )
    post_history(ledger)
    account = await link(client, ledger)

    await synced(client, account["id"])

    assert threads
    assert threading.get_ident() not in threads


@pytest.mark.parametrize(
    "posted_at",
    [
        datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-1))),
        datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))),
    ],
    ids=["after-year-9999-in-utc", "before-year-1-in-utc"],
)
async def test_posting_time_outside_the_calendar_is_an_invalid_response(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger, posted_at: datetime
) -> None:
    # Valid aware datetimes whose UTC date does not exist: astimezone raised OverflowError (500).
    ledger.post("deposit", 100, at=posted_at)
    account = await link(client, ledger)

    response = await sync(client, account["id"])

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ledger_invalid_response"
    assert await stored(session, account["id"]) == (0, 0)


async def test_sync_without_new_entries_records_nothing(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    # A client polling for news must not grow ledger_syncs by a row per poll (the reason failed
    # syncs are not recorded either, ADR 0009).
    post_history(ledger)
    account = await link(client, ledger)
    await synced(client, account["id"])

    response = await sync(client, account["id"])

    assert response.status_code == 204
    assert response.content == b""
    assert "location" not in response.headers
    assert await stored(session, account["id"]) == (1, 5)


async def test_ledger_currency_outside_the_known_list_is_refused(
    session: AsyncSession, client: httpx2.AsyncClient, ledger: FakeLedger
) -> None:
    # USD is a currency here, but not one the ledger offers: its minor unit there is unknown, and
    # amounts are taken over as they are (ADR 0001), so it is not guessed.
    ledger.currency = "USD"
    post_history(ledger)
    account = await link(client, ledger)

    response = await sync(client, account["id"])

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ledger_invalid_response"
    assert await stored(session, account["id"]) == (0, 0)
