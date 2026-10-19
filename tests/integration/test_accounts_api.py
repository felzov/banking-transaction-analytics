"""Accounts over HTTP against the migrated database."""

import uuid

import httpx2
import pytest
from factories import OTHER_OWNER_ID, OWNER_ID
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.api.deps import get_owner_id
from transaction_analytics.data.models import Account

pytestmark = pytest.mark.integration


def act_as(app: FastAPI, owner_id: uuid.UUID) -> None:
    app.dependency_overrides[get_owner_id] = lambda: owner_id


async def create(client: httpx2.AsyncClient, name: str = "Current account") -> dict:
    response = await client.post("/accounts", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_normalizes_the_name_and_reads_back(client: httpx2.AsyncClient) -> None:
    created = await create(client, "  Current\u00a0\t account ")

    assert created["name"] == "Current account"
    uuid.UUID(created["id"])
    assert created["created_at"]

    response = await client.get(f"/accounts/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


async def test_name_with_emoji_and_rtl_mark_round_trips(client: httpx2.AsyncClient) -> None:
    name = "Family \U0001f468\u200d\U0001f469 \u200f\u062d\u0633\u0627\u0628"

    assert (await create(client, name))["name"] == name


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("", "name_required"),
        ("   ", "name_required"),
        ("\u00a0\t\n", "name_required"),
        ("\u200b\u2060", "name_required"),
        ("x" * 101, "name_too_long"),
        ("Secret\x00Name", "name_invalid_characters"),
    ],
)
async def test_invalid_name_is_rejected_without_echo(
    client: httpx2.AsyncClient, session: AsyncSession, name: str, code: str
) -> None:
    response = await client.post("/accounts", json={"name": name})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    if name.strip():
        assert name.strip() not in response.text
    assert await session.scalar(select(func.count()).select_from(Account)) == 0


async def test_name_of_exactly_100_characters_is_accepted(client: httpx2.AsyncClient) -> None:
    assert len((await create(client, "x" * 100))["name"]) == 100


@pytest.mark.parametrize(
    "body",
    [
        {"name": "SECRETNAME" * 101},  # over the request bound, before normalization
        {"name": "Current", "owner_id": str(OTHER_OWNER_ID)},  # extra field
        {},
    ],
    ids=["too-long-input", "extra-field", "missing-name"],
)
async def test_malformed_body_is_rejected_without_echo(
    client: httpx2.AsyncClient, body: dict
) -> None:
    response = await client.post("/accounts", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "SECRETNAME" not in response.text
    assert str(OTHER_OWNER_ID) not in response.text


async def test_list_shows_only_the_owners_accounts(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    mine = {(await create(client, name))["id"] for name in ("Current", "Savings")}
    act_as(app, OTHER_OWNER_ID)
    theirs = (await create(client, "Other"))["id"]

    act_as(app, OWNER_ID)
    listed = {account["id"] for account in (await client.get("/accounts")).json()}

    assert listed == mine
    assert theirs not in listed


async def test_another_owners_account_is_not_found(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    account_id = (await create(client))["id"]

    act_as(app, OTHER_OWNER_ID)
    response = await client.get(f"/accounts/{account_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "account_not_found", "message": "Account not found."}
    }


async def test_unknown_and_malformed_account_ids(client: httpx2.AsyncClient) -> None:
    unknown = await client.get(f"/accounts/{uuid.uuid4()}")
    malformed = await client.get("/accounts/not-a-uuid")

    assert unknown.status_code == 404
    assert malformed.status_code == 422
    assert malformed.json()["error"]["details"] == [
        {"loc": ["path", "account_id"], "type": "uuid_parsing"}
    ]
    assert "not-a-uuid" not in malformed.text
