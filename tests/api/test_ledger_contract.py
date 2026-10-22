"""Ledger sync requests refused before any query or ledger call (ADR 0009).

The session is a _NoDatabase: a malformed body or token, or a server without a ledger URL, must be
answered without touching the database, and nothing the client sent may come back or be logged.
Syncs against a (fake) ledger and the database are in tests/integration/test_ledger_sync_api.py.
"""

import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transaction_analytics.api.deps import get_ledger_settings, get_owner_id, get_session
from transaction_analytics.main import create_app

MARKER = "LEAK-ME-5120"
OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SYNC_URL = "/accounts/00000000-0000-4000-8000-0000000000aa/ledger-syncs"
TOKEN = "lt_" + "T" * 43


class _NoDatabase:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"database used: {name}")


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_session] = _NoDatabase
    app.dependency_overrides[get_owner_id] = lambda: OWNER_ID
    app.dependency_overrides[get_ledger_settings] = lambda: None
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _app_logs(caplog: pytest.LogCaptureFixture) -> str:
    # The test client logs the URLs it requests; everything the app logs must be clean.
    return "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx2")


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({}, ["body", "ledger_token"]),
        ({"ledger_token": 46}, ["body", "ledger_token"]),
        ({"ledger_token": "lt_" + MARKER * 40}, ["body", "ledger_token"]),  # over the bound
        ({"ledger_token": TOKEN, MARKER: 1}, ["body", "<extra>"]),
    ],
)
def test_malformed_sync_request_is_422_without_echo(
    client: TestClient, caplog: pytest.LogCaptureFixture, body: dict, loc: list[str]
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.post(SYNC_URL, json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert [detail["loc"] for detail in error["details"]] == [loc]
    assert MARKER not in response.text
    assert MARKER not in _app_logs(caplog)


@pytest.mark.parametrize(
    "token",
    [
        MARKER,
        "bta_" + "A" * 43,  # this service's own token instead of the ledger's
        "lt_" + "A" * 42,
        "lt_" + "é" * 43,  # would not even fit in an HTTP header
        "lt_" + "A" * 43 + "\r\nX-Injected: 1",
    ],
)
def test_token_of_the_wrong_shape_is_refused_before_anything_else(
    client: TestClient, caplog: pytest.LogCaptureFixture, token: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.post(SYNC_URL, json={"ledger_token": token})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_ledger_token"
    assert MARKER not in response.text
    assert MARKER not in _app_logs(caplog)


def test_sync_without_a_configured_ledger_is_503(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.post(SYNC_URL, json={"ledger_token": TOKEN})

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "ledger_not_configured",
            "message": "Ledger syncs are not configured on this server.",
        }
    }
    assert TOKEN not in _app_logs(caplog)


@pytest.mark.parametrize(
    "ledger",
    [
        {"user_id": MARKER, "account_id": str(uuid.uuid4())},
        {"user_id": str(uuid.uuid4())},
        {"user_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "currency": "GBP"},
        MARKER,
    ],
)
def test_malformed_ledger_link_is_422_without_echo(client: TestClient, ledger: object) -> None:
    response = client.post("/accounts", json={"name": "Ledger", "ledger": ledger})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert MARKER not in response.text


def test_openapi_marks_the_ledger_token_as_a_secret(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    token = schemas["LedgerSyncCreate"]["properties"]["ledger_token"]

    assert token["format"] == "password"
    assert token.get("writeOnly") is True
