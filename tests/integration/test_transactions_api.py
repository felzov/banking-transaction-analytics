"""Transaction listing and summaries end to end: HTTP -> service -> PostgreSQL (ADR 0008).

Numbers and edge cases of the queries are in test_transaction_reads.py; these tests pin the HTTP
shapes, the money format, and how imports (with deduplication) feed the results.
"""

import uuid
from datetime import date
from pathlib import Path

import factories
import httpx2
import pytest
from factories import OTHER_OWNER_ID
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.domain.money import MAX_ABS_AMOUNT_MINOR, Currency

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"
VALID = (FIXTURES / "statement_valid.csv").read_bytes()
HEADER = b"date,description,amount,currency\n"


async def new_account(client: httpx2.AsyncClient) -> str:
    response = await client.post("/accounts", json={"name": "Current account"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def upload(client: httpx2.AsyncClient, account_id: str, content: bytes) -> dict:
    response = await client.post(
        f"/accounts/{account_id}/imports", content=content, headers={"Content-Type": "text/csv"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def get_ok(client: httpx2.AsyncClient, url: str, **params: str) -> dict:
    response = await client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_imported_statement_is_listed_newest_first(client: httpx2.AsyncClient) -> None:
    account_id = await new_account(client)
    job = await upload(client, account_id, VALID)

    body = await get_ok(client, f"/accounts/{account_id}/transactions")

    items = body["items"]
    assert body["next_cursor"] is None
    assert [(t["booked_on"], t["description"], t["amount"]) for t in items] == [
        ("2026-09-04", "Hotel Berlin", "-120.50"),
        ("2026-09-03", "Grocer, High Street", "-45.67"),
        ("2026-09-02", "Coffee Shop", "-3.20"),  # whitespace collapsed by the parser
        ("2026-09-02", "Coffee Shop", "-3.20"),  # two coffees on one day: both kept (ADR 0002)
        ("2026-09-01", "SALARY ACME LTD", "2500.00"),
    ]
    first = items[0]
    assert set(first) == {
        "id",
        "source",
        "import_id",
        "ledger_entry_id",
        "booked_on",
        "description",
        "currency",
        "amount_minor",
        "amount",
    }
    assert first["import_id"] == job["id"]
    assert (first["source"], first["ledger_entry_id"]) == ("csv", None)
    assert (first["currency"], first["amount_minor"]) == ("EUR", -12050)
    uuid.UUID(first["id"])


async def test_summary_of_an_imported_statement(client: httpx2.AsyncClient) -> None:
    account_id = await new_account(client)
    await upload(client, account_id, VALID)

    body = await get_ok(client, f"/accounts/{account_id}/summary")

    assert body == {
        "account_id": account_id,
        "from": None,
        "to": None,
        "totals": [
            {
                "currency": "EUR",
                "transactions": 1,
                "inflow_minor": 0,
                "inflow": "0.00",
                "outflow_minor": -12050,
                "outflow": "-120.50",
                "net_minor": -12050,
                "net": "-120.50",
            },
            {
                "currency": "GBP",
                "transactions": 4,
                "inflow_minor": 250000,
                "inflow": "2500.00",
                "outflow_minor": -5207,
                "outflow": "-52.07",
                "net_minor": 244793,
                "net": "2447.93",
            },
        ],
    }


async def test_overlapping_statement_is_counted_once(client: httpx2.AsyncClient) -> None:
    # The second export repeats 2-3 September and adds the 5th: analytics must see the dedup
    # result (ADR 0006), not two copies of the overlap.
    account_id = await new_account(client)
    await upload(
        client, account_id, HEADER + b"2026-09-02,Rent,-950.00,GBP\n2026-09-03,Pay,10,GBP\n"
    )
    second = await upload(
        client,
        account_id,
        HEADER + b"2026-09-02,Rent,-950.00,GBP\n2026-09-03,Pay,10,GBP\n2026-09-05,Tea,-2,GBP\n",
    )
    assert (second["duplicate_rows"], second["imported_rows"]) == (2, 1)

    body = await get_ok(client, f"/accounts/{account_id}/summary")

    (gbp,) = body["totals"]
    assert (gbp["transactions"], gbp["inflow_minor"], gbp["outflow_minor"]) == (3, 1000, -95200)


async def test_monthly_summary_shape_and_range(client: httpx2.AsyncClient) -> None:
    account_id = await new_account(client)
    await upload(
        client,
        account_id,
        HEADER
        + b"2026-08-31,A,-1.00,GBP\n2026-09-01,B,-2.00,GBP\n2026-09-15,C,500,JPY\n"
        + b"2026-10-01,D,3.00,GBP\n",
    )

    body = await get_ok(
        client,
        f"/accounts/{account_id}/summary/monthly",
        **{"from": "2026-09-01", "to": "2026-10-31"},
    )

    assert body["from"] == "2026-09-01"
    assert body["to"] == "2026-10-31"
    assert [(m["month"], m["currency"], m["net"]) for m in body["months"]] == [
        ("2026-09", "GBP", "-2.00"),
        ("2026-09", "JPY", "500"),
        ("2026-10", "GBP", "3.00"),
    ]
    assert set(body["months"][0]) == {
        "month",
        "currency",
        "transactions",
        "inflow_minor",
        "inflow",
        "outflow_minor",
        "outflow",
        "net_minor",
        "net",
    }


async def test_sums_beyond_bigint_survive_json(
    client: httpx2.AsyncClient, session: AsyncSession
) -> None:
    account = factories.account()
    job = factories.completed(account, total_rows=2, valid_rows=2, invalid_rows=0)
    session.add_all([account, job])
    await session.flush()
    session.add_all(
        factories.transaction(
            job, row_number=n, amount_minor=MAX_ABS_AMOUNT_MINOR, currency=Currency.USD
        )
        for n in (2, 3)
    )
    await session.flush()

    body = await get_ok(client, f"/accounts/{account.id}/summary")

    (usd,) = body["totals"]
    assert usd["inflow_minor"] == 2 * MAX_ABS_AMOUNT_MINOR  # an exact JSON integer
    assert usd["inflow"] == "184467440737095516.14"
    assert usd["net"] == usd["inflow"]


async def test_following_next_cursor_lists_every_transaction_once(
    client: httpx2.AsyncClient,
) -> None:
    account_id = await new_account(client)
    rows = b"".join(b"2026-09-0%d,Item %d,-1.00,GBP\n" % (1 + n % 3, n) for n in range(10))
    await upload(client, account_id, HEADER + rows)

    seen: list[str] = []
    params = {"limit": "4"}
    while True:
        body = await get_ok(client, f"/accounts/{account_id}/transactions", **params)
        seen += [t["id"] for t in body["items"]]
        if body["next_cursor"] is None:
            break
        params["cursor"] = body["next_cursor"]

    assert len(seen) == len(set(seen)) == 10


async def test_range_filters_the_listing(client: httpx2.AsyncClient) -> None:
    account_id = await new_account(client)
    await upload(client, account_id, VALID)

    body = await get_ok(
        client, f"/accounts/{account_id}/transactions", **{"from": "2026-09-02", "to": "2026-09-03"}
    )

    assert [t["booked_on"] for t in body["items"]] == ["2026-09-03", "2026-09-02", "2026-09-02"]


@pytest.mark.parametrize("suffix", ["transactions", "summary", "summary/monthly"])
async def test_another_owners_account_is_not_found(
    client: httpx2.AsyncClient, session: AsyncSession, suffix: str
) -> None:
    foreign = factories.account(OTHER_OWNER_ID)
    session.add(foreign)
    await session.flush()

    for account_id in (foreign.id, uuid.uuid4()):  # someone else's, and one that does not exist
        response = await client.get(f"/accounts/{account_id}/{suffix}")

        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "account_not_found", "message": "Account not found."}
        }


async def test_one_day_range_on_an_empty_account(client: httpx2.AsyncClient) -> None:
    account_id = await new_account(client)
    day = date(2026, 9, 1).isoformat()

    summary = await get_ok(client, f"/accounts/{account_id}/summary", **{"from": day, "to": day})
    listing = await get_ok(client, f"/accounts/{account_id}/transactions")

    assert summary["totals"] == []
    assert listing == {"items": [], "next_cursor": None}
