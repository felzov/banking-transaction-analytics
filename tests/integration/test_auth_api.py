"""Authentication end to end: real API tokens, no get_owner_id override (ADR 0007).

Setup data is committed (a savepoint release here) before each request, as it would be by another
client: services commit or roll back their own transactions.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from factories import api_token
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from transaction_analytics.api.deps import get_owner_id
from transaction_analytics.data.models import Account, ApiToken, ImportJob
from transaction_analytics.domain.auth import token_digest
from transaction_analytics.domain.csv_parser import ParseResult
from transaction_analytics.services import auth as auth_service
from transaction_analytics.services import imports as import_service
from transaction_analytics.services.auth import generate_token

pytestmark = pytest.mark.integration

VALID = (Path(__file__).parents[1] / "fixtures" / "statement_valid.csv").read_bytes()
NOT_AUTHENTICATED = {
    "error": {"code": "not_authenticated", "message": "A valid API token is required."}
}


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The conftest app, with real authentication."""
    del app.dependency_overrides[get_owner_id]
    return app


async def new_user(session: AsyncSession, name: str = "Alice") -> tuple[uuid.UUID, str]:
    """A committed user and a live token for it."""
    user = await auth_service.create_user(session, name=name)
    issued = await auth_service.issue_token(session, owner_id=user.id, name="laptop", days=30)
    return user.id, issued.plaintext


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def add_token_expiring(session: AsyncSession, owner_id: uuid.UUID, offset: timedelta) -> str:
    """A token whose expiry is the database's now() plus offset (the test transaction's now())."""
    token = generate_token()
    session.add(
        api_token(
            owner_id,
            token_sha256=token_digest(token),
            created_at=func.now() - timedelta(days=1),
            expires_at=func.now() + offset,
        )
    )
    await session.commit()
    return token


async def test_a_valid_token_acts_as_its_user(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    user_id, token = await new_user(session)

    me = await client.get("/me", headers=bearer(token))
    created = await client.post("/accounts", json={"name": "Current"}, headers=bearer(token))

    assert me.status_code == 200
    assert me.json()["id"] == str(user_id)
    assert me.json()["name"] == "Alice"
    assert created.status_code == 201
    stored = await session.get(Account, uuid.UUID(created.json()["id"]))
    assert stored is not None
    assert stored.owner_id == user_id


async def test_scheme_is_case_insensitive(client: httpx.AsyncClient, session: AsyncSession) -> None:
    _, token = await new_user(session)

    response = await client.get("/me", headers={"Authorization": f"bearer {token}"})

    assert response.status_code == 200


async def _unknown(session: AsyncSession) -> str:
    await new_user(session)  # a live token exists, just not this one
    return generate_token()


async def _revoked(session: AsyncSession) -> str:
    user_id, token = await new_user(session)
    (issued,) = await auth_service.list_tokens(session, owner_id=user_id)
    await auth_service.revoke_token(session, owner_id=user_id, token_id=issued.id)
    return token


async def _expired(session: AsyncSession) -> str:
    user_id, _ = await new_user(session)
    return await add_token_expiring(session, user_id, timedelta(0))


async def _disabled_user(session: AsyncSession) -> str:
    user_id, token = await new_user(session)
    await auth_service.disable_user(session, owner_id=user_id)
    return token


@pytest.mark.parametrize("make_token", [_unknown, _revoked, _expired, _disabled_user])
async def test_every_dead_token_gets_the_same_401(
    client: httpx.AsyncClient, session: AsyncSession, make_token: object
) -> None:
    # One body and header for every reason, so a caller cannot learn which tokens exist.
    token = await make_token(session)  # type: ignore[operator]

    response = await client.get("/accounts", headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    # RFC 6750: a token was sent, so invalid_token; the same value whatever the reason.
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_no_token_gets_no_error_code(client: httpx.AsyncClient) -> None:
    # RFC 6750 section 3.1: a request without credentials gets the scheme only.
    for headers in ({}, {"Authorization": "Basic dXNlcjpwYXNz"}):
        response = await client.get("/me", headers=headers)

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


async def test_authentication_keeps_the_request_transaction(session: AsyncSession) -> None:
    # Review fix: ending the lookup's transaction on every request cost a ROLLBACK, a pool checkout
    # with pre-ping and a new BEGIN. Only the upload route ends it (test below).
    _, token = await new_user(session)

    user = await auth_service.authenticate(session, token=token)

    assert session.in_transaction()
    assert user.name == "Alice"


async def test_me_is_answered_by_the_token_lookup_alone(
    client: httpx.AsyncClient, session: AsyncSession, engine: AsyncEngine
) -> None:
    # Review fix: /me loaded the user a second time after authenticating.
    _, token = await new_user(session)
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get("/me", headers=bearer(token))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    # SAVEPOINT is how the test session begins a transaction (conftest), not a query of the app's.
    queries = [s for s in statements if not s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO"))]
    assert response.status_code == 200
    assert len(queries) == 1
    assert queries[0].lstrip().startswith("SELECT users.")


async def test_expiry_is_exclusive_and_uses_the_database_clock(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    # now() is fixed for the whole test transaction, so the boundary is exact.
    user_id, _ = await new_user(session)
    expiring_now = await add_token_expiring(session, user_id, timedelta(0))
    one_microsecond_left = await add_token_expiring(session, user_id, timedelta(microseconds=1))

    assert (await client.get("/me", headers=bearer(expiring_now))).status_code == 401
    assert (await client.get("/me", headers=bearer(one_microsecond_left))).status_code == 200


async def test_revoking_takes_effect_on_the_next_request(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    user_id, token = await new_user(session)
    assert (await client.get("/me", headers=bearer(token))).status_code == 200
    (issued,) = await auth_service.list_tokens(session, owner_id=user_id)

    await auth_service.revoke_token(session, owner_id=user_id, token_id=issued.id)

    assert (await client.get("/me", headers=bearer(token))).status_code == 401


async def test_users_cannot_reach_each_others_data(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    _, alice = await new_user(session, "Alice")
    _, bob = await new_user(session, "Bob")
    account = await client.post("/accounts", json={"name": "Current"}, headers=bearer(alice))
    account_id = account.json()["id"]
    imported = await client.post(
        f"/accounts/{account_id}/imports",
        content=VALID,
        headers={**bearer(alice), "Content-Type": "text/csv"},
    )
    import_id = imported.json()["id"]

    listed = await client.get("/accounts", headers=bearer(bob))
    read_account = await client.get(f"/accounts/{account_id}", headers=bearer(bob))
    upload = await client.post(
        f"/accounts/{account_id}/imports",
        content=b"date,description,amount,currency\n2026-09-09,Bob,-1.00,GBP\n",
        headers={**bearer(bob), "Content-Type": "text/csv"},
    )
    read_import = await client.get(f"/imports/{import_id}", headers=bearer(bob))
    read_errors = await client.get(f"/imports/{import_id}/errors", headers=bearer(bob))

    assert listed.json() == []
    assert [r.status_code for r in (read_account, upload, read_import, read_errors)] == [404] * 4
    assert upload.json()["error"]["code"] == "account_not_found"
    assert await session.scalar(select(func.count()).select_from(ImportJob)) == 1
    # Alice still sees her own data.
    assert (await client.get(f"/imports/{import_id}", headers=bearer(alice))).status_code == 200


async def test_unauthenticated_upload_reads_no_body_and_stores_nothing(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await new_user(session)
    account = await client.post("/accounts", json={"name": "Current"}, headers=bearer(token))
    read: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        read.append(VALID)
        yield VALID

    def parser_must_not_run(content: bytes, **limits: int) -> ParseResult:
        raise AssertionError("parsed without authentication")

    monkeypatch.setattr(import_service, "parse_statement_csv", parser_must_not_run)

    response = await client.post(
        f"/accounts/{account.json()['id']}/imports",
        content=body(),
        headers={"Content-Type": "text/csv"},
    )

    assert response.status_code == 401
    assert read == []
    assert await session.scalar(select(func.count()).select_from(ImportJob)) == 0


async def test_no_transaction_is_open_while_the_body_is_read_or_parsed(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Authentication runs before the body is streamed; its read transaction must be over by then,
    # or a slow upload would hold a pooled connection "idle in transaction" (ADR 0004, item 10).
    _, token = await new_user(session)
    account = await client.post("/accounts", json={"name": "Current"}, headers=bearer(token))
    in_transaction: dict[str, bool] = {}
    real_parser = import_service.parse_statement_csv

    async def body() -> AsyncIterator[bytes]:
        in_transaction["reading body"] = session.in_transaction()
        yield VALID

    def recording_parser(content: bytes, **limits: int) -> ParseResult:
        in_transaction["parsing"] = session.in_transaction()
        return real_parser(content, **limits)

    monkeypatch.setattr(import_service, "parse_statement_csv", recording_parser)

    response = await client.post(
        f"/accounts/{account.json()['id']}/imports",
        content=body(),
        headers={**bearer(token), "Content-Type": "text/csv"},
    )

    assert response.status_code == 201
    assert in_transaction == {"reading body": False, "parsing": False}


async def test_only_the_hash_of_a_token_is_stored(session: AsyncSession) -> None:
    user_id, token = await new_user(session)

    (stored,) = await session.scalars(select(ApiToken).where(ApiToken.owner_id == user_id))

    assert stored.token_sha256 == token_digest(token)
    values = [getattr(stored, column.key) for column in ApiToken.__table__.columns]
    assert token not in [value for value in values if isinstance(value, str)]


async def test_tokens_never_reach_logs_or_responses(
    client: httpx.AsyncClient, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    _, live = await new_user(session)
    revoked = await _revoked(session)

    with caplog.at_level(logging.DEBUG):
        responses = [
            await client.get("/me", headers=bearer(live)),
            await client.get("/me", headers=bearer(revoked)),
            await client.get("/accounts/not-a-uuid", headers=bearer(live)),
        ]

    for token in (live, revoked):
        assert token not in caplog.text
        assert all(token not in response.text for response in responses)
