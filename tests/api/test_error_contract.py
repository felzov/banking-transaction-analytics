"""HTTP error contract without a database (ADR 0005).

TestClient re-raises any exception that escapes the app (raise_server_exceptions=True), which is
exactly what a real server would log with its full text. So "the request returned a 500" here also
proves nothing reached the server's exception logging.
"""

import logging
from collections.abc import Iterator
from typing import Any

import fastapi.dependencies.utils as fastapi_dependency_utils
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError

from transaction_analytics.api.deps import (
    get_app_settings,
    get_import_limits,
    get_owner_id,
    get_session,
)
from transaction_analytics.api.errors import SERVICE_ERROR_STATUS
from transaction_analytics.api.imports import router as imports_router
from transaction_analytics.api.limits import MAX_JSON_BYTES, is_upload_path
from transaction_analytics.main import create_app
from transaction_analytics.services.errors import ServiceError
from transaction_analytics.services.imports import ImportLimits
from transaction_analytics.settings import DEFAULT_DEV_OWNER_ID, Settings

MARKER = "LEAK-ME-9012"
MAX_BYTES = 64
IMPORT_URL = "/accounts/00000000-0000-4000-8000-0000000000aa/imports"


class _NoDatabase:
    """Any use means a test reached the database it should never need."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"database used: {name}")


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_session] = _NoDatabase
    app.dependency_overrides[get_owner_id] = lambda: DEFAULT_DEV_OWNER_ID
    app.dependency_overrides[get_import_limits] = lambda: ImportLimits(
        max_bytes=MAX_BYTES, max_rows=10
    )

    @app.get("/boom/database")
    def database_error() -> None:
        # str() of this exception contains the marker, as a real asyncpg DataError would.
        raise DBAPIError("INSERT ...", {"description": MARKER}, Exception(f"value {MARKER}"))

    @app.get("/boom/plain")
    def plain_error() -> None:
        raise RuntimeError(f"bug with {MARKER}")

    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    yield TestClient(app)


@pytest.mark.parametrize("path", ["/boom/database", "/boom/plain"])
def test_unhandled_error_is_a_fixed_500_and_logs_no_text(
    client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.get(path)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal server error."}
    }
    assert MARKER not in caplog.text
    assert "Unhandled" in caplog.text


def test_validation_error_has_location_and_type_but_no_input(client: TestClient) -> None:
    response = client.post("/accounts", json={"name": MARKER * 200})

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "Request is invalid.",
            "details": [{"loc": ["body", "name"], "type": "string_too_long"}],
        }
    }


def test_invalid_json_is_rejected_without_echo(client: TestClient) -> None:
    response = client.post(
        "/accounts",
        content=b'{"name": "' + MARKER.encode(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert MARKER not in response.text


def _explode_in_a_helper() -> None:
    raise RuntimeError(f"bug with {MARKER}")


def test_unhandled_error_log_says_where_without_the_message(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    @app.get("/boom/located")
    def located() -> None:
        _explode_in_a_helper()

    with caplog.at_level(logging.DEBUG):
        response = TestClient(app).get("/boom/located")

    assert response.status_code == 500
    assert "_explode_in_a_helper" in caplog.text  # where it failed: file, line, function
    assert "test_error_contract.py" in caplog.text
    assert MARKER not in caplog.text


async def test_client_disconnect_during_upload_is_not_an_error(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    messages = [
        {"type": "http.request", "body": b"date,description", "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": IMPORT_URL,
        "raw_path": IMPORT_URL.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"text/csv")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    with caplog.at_level(logging.DEBUG):
        await app(scope, receive, send)

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert sent == []  # nobody is listening any more


def test_nonstandard_http_status_keeps_its_code(app: FastAPI) -> None:
    @app.get("/teapot-ish")
    def nonstandard() -> None:
        raise HTTPException(status_code=499)

    response = TestClient(app).get("/teapot-ish")

    assert response.status_code == 499
    assert response.json()["error"]["code"] == "http_error"


@pytest.mark.parametrize(
    "send",
    [
        lambda client: client.post("/accounts", json={"name": "x" * (MAX_JSON_BYTES + 1)}),
        lambda client: client.post(
            "/accounts",
            content=iter([b'{"name": "', b"x" * (MAX_JSON_BYTES + 1), b'"}']),
            headers={"Content-Type": "application/json"},
        ),
    ],
    ids=["declared", "chunked"],
)
def test_oversized_json_body_is_refused_before_it_is_read(client: TestClient, send: Any) -> None:
    response = send(client)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_json_body_within_the_limit_reaches_validation(client: TestClient) -> None:
    # Just under the limit: the body is replayed to the app intact, so validation runs on it.
    name = "x" * (MAX_JSON_BYTES - 100)

    response = client.post("/accounts", json={"name": name})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["type"] == "string_too_long"


def test_the_upload_route_is_exempt_from_the_json_limit() -> None:
    # The CSV upload has its own, larger limit (ImportLimits). If the route path changes, the
    # middleware's exemption must change with it.
    (route,) = [r for r in imports_router.routes if getattr(r, "name", None) == "upload_statement"]
    concrete = route.path.replace("{account_id}", "00000000-0000-4000-8000-0000000000aa")

    assert is_upload_path(concrete)
    assert not is_upload_path("/accounts")


def test_unknown_route_uses_the_error_envelope(client: TestClient) -> None:
    response = client.get("/nowhere")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_wrong_method_keeps_the_allow_header(client: TestClient) -> None:
    # /health has one route. Where GET and POST are separate routes on one path (/accounts),
    # Starlette's Allow lists only the first route's method; that is a router limitation.
    response = client.delete("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


def test_every_service_error_has_an_http_status() -> None:
    assert set(ServiceError.__subclasses__()) == set(SERVICE_ERROR_STATUS)


# --- upload checks that happen before any service or database call ------------------------------


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "text/csvx"])
def test_upload_with_wrong_content_type_is_415(client: TestClient, content_type: str) -> None:
    response = client.post(IMPORT_URL, content=b"a", headers={"Content-Type": content_type})

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_upload_without_content_type_is_415(client: TestClient) -> None:
    assert client.post(IMPORT_URL, content=b"a").status_code == 415


def test_upload_over_declared_limit_is_413(client: TestClient) -> None:
    response = client.post(
        IMPORT_URL, content=b"x" * (MAX_BYTES + 1), headers={"Content-Type": "text/csv"}
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "payload_too_large", "message": f"File is larger than {MAX_BYTES} bytes."}
    }


def test_chunked_upload_over_limit_is_413(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * MAX_BYTES
        yield b"x"

    response = client.post(IMPORT_URL, content=chunks(), headers={"Content-Type": "text/csv"})

    assert response.status_code == 413


def test_upload_with_understated_content_length_is_413(client: TestClient) -> None:
    # The declared length is checked first, but only the counted stream is trusted.
    response = client.post(
        IMPORT_URL,
        content=b"x" * (MAX_BYTES + 1),
        headers={"Content-Type": "text/csv", "Content-Length": "1"},
    )

    assert response.status_code == 413


def test_unknown_json_key_is_not_echoed(client: TestClient) -> None:
    # pydantic puts the client's key into loc for extra_forbidden; a key is client text too.
    response = client.post("/accounts", json={"name": "Current", MARKER: 1})

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"loc": ["body", "<extra>"], "type": "extra_forbidden"}
    ]
    assert MARKER not in response.text


async def test_request_path_uses_no_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    # FastAPI runs sync dependencies (and sync endpoints) in its threadpool: a thread hop per
    # request for nothing, competing with the parser's worker threads. Observed, not inspected.
    calls: list[str] = []
    real = fastapi_dependency_utils.run_in_threadpool

    async def recording(function: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(function, "__name__", repr(function)))
        return await real(function, *args, **kwargs)

    monkeypatch.setattr(fastapi_dependency_utils, "run_in_threadpool", recording)
    app = create_app()

    async def settings() -> Settings:
        return Settings(database_url=SecretStr("postgresql+asyncpg://unused/unused"))

    async def no_database() -> _NoDatabase:
        return _NoDatabase()

    # Only leaf dependencies are replaced, with async ones; get_owner_id and get_import_limits
    # run for real. The 415 is raised after every dependency has been solved.
    app.dependency_overrides[get_app_settings] = settings
    app.dependency_overrides[get_session] = no_database
    response = TestClient(app).post(IMPORT_URL, content=b"a", headers={"Content-Type": "x/y"})

    assert response.status_code == 415
    assert calls == []
