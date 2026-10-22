"""Authentication at the HTTP edge, without a database (ADR 0007).

Here get_owner_id runs for real. Every request below lacks a usable token, so it must be refused
before any query: the session is a _NoDatabase that fails the test on first use. Tokens that are
well formed but unknown, expired or revoked need the database: tests/integration/test_auth_api.py.
"""

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transaction_analytics.api.deps import get_import_limits, get_session
from transaction_analytics.main import create_app
from transaction_analytics.services.imports import ImportLimits

MARKER = "LEAK-ME-7531"
SOME_ID = "00000000-0000-4000-8000-0000000000aa"
NOT_AUTHENTICATED = {
    "error": {"code": "not_authenticated", "message": "A valid API token is required."}
}

# Routes anyone may call. Everything else must depend on get_owner_id. (/docs and /openapi.json
# are not API routes and are always public: they describe the API, not anyone's data.)
PUBLIC_PATHS = {"/health"}


class _NoDatabase:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"database used: {name}")

    def in_transaction(self) -> bool:
        # The one question that needs no database: the upload route asks it before streaming
        # (services.imports.release_connection). Nothing was queried, so no.
        return False


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_session] = _NoDatabase
    app.dependency_overrides[get_import_limits] = lambda: ImportLimits(max_bytes=64, max_rows=10)
    return app


def _operations(app: FastAPI) -> dict[tuple[str, str], dict]:
    """(METHOD, path template) -> OpenAPI operation, for every route in the schema."""
    return {
        (method.upper(), path): operation
        for path, item in app.openapi()["paths"].items()
        for method, operation in item.items()
    }


def _protected(app: FastAPI) -> list[tuple[str, str]]:
    """(method, concrete path) for every route that needs a token."""
    return [
        (method, re.sub(r"\{[^}]+\}", SOME_ID, path))
        for method, path in _operations(app)
        if path not in PUBLIC_PATHS
    ]


def test_every_route_but_the_public_ones_requires_a_token(app: FastAPI) -> None:
    # The schema marks an operation with the bearer scheme exactly when it depends on it, which
    # only get_owner_id does. A new route without OwnerIdDep fails here rather than shipping
    # unauthenticated. (Built from the public schema: FastAPI's own route tree is private API.)
    for (method, path), operation in _operations(app).items():
        expected = [] if path in PUBLIC_PATHS else [{"HTTPBearer": []}]
        assert operation.get("security", []) == expected, (method, path)


def test_every_route_is_in_the_schema(app: FastAPI) -> None:
    # A route hidden with include_in_schema=False would escape the check above; pin the full list.
    assert sorted(_operations(app)) == [
        ("GET", "/accounts"),
        ("GET", "/accounts/{account_id}"),
        ("GET", "/accounts/{account_id}/summary"),
        ("GET", "/accounts/{account_id}/summary/monthly"),
        ("GET", "/accounts/{account_id}/transactions"),
        ("GET", "/health"),
        ("GET", "/imports/{import_id}"),
        ("GET", "/imports/{import_id}/errors"),
        ("GET", "/ledger-syncs/{sync_id}"),
        ("GET", "/me"),
        ("POST", "/accounts"),
        ("POST", "/accounts/{account_id}/imports"),
        ("POST", "/accounts/{account_id}/ledger-syncs"),
    ]


def test_every_protected_route_without_a_token_is_401(app: FastAPI) -> None:
    client = TestClient(app)
    for method, path in _protected(app):
        response = client.request(method, path)

        assert response.status_code == 401, (method, path)
        assert response.json() == NOT_AUTHENTICATED
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [
        b"",
        b"Bearer",
        b"Bearer ",
        b"Basic dXNlcjpwYXNzd29yZA==",
        b"Token bta_" + b"A" * 43,  # right token, wrong scheme
        b"Bearer " + MARKER.encode(),
        b"Bearer bta_" + b"A" * 42,
        b"Bearer bta_" + b"A" * 44,
        b"Bearer bta_" + b"A" * 42 + b"=",
        b"Bearer bta_" + "é".encode("latin-1") * 43,  # header bytes are latin-1, not ASCII
        b"Bearer bta_" + b"A" * 43 + b" bta_" + b"B" * 43,  # two tokens
    ],
)
def test_unusable_authorization_is_401_without_a_query(
    app: FastAPI, caplog: pytest.LogCaptureFixture, authorization: bytes
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = TestClient(app).get("/accounts", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert MARKER not in response.text
    assert MARKER not in caplog.text


def test_scheme_is_case_insensitive(app: FastAPI) -> None:
    # "bearer" is accepted (RFC 9110); the malformed token then fails without a query.
    response = TestClient(app).get("/accounts", headers={"Authorization": "bearer bta_short"})

    assert response.status_code == 401


def test_unauthenticated_upload_is_refused_before_its_body_is_checked(app: FastAPI) -> None:
    # Authentication is a dependency, solved before the route streams the body: no 413 or 415
    # for strangers, and an oversized body is never read.
    client = TestClient(app)
    oversized = client.post(
        f"/accounts/{SOME_ID}/imports", content=b"x" * 1000, headers={"Content-Type": "text/csv"}
    )
    wrong_type = client.post(f"/accounts/{SOME_ID}/imports", content=b"a")

    assert oversized.status_code == 401
    assert wrong_type.status_code == 401


def test_public_routes_need_no_token(app: FastAPI) -> None:
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_openapi_documents_the_bearer_scheme(app: FastAPI) -> None:
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/me"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]
