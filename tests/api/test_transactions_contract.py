"""Transaction listing and summary queries that are refused before any query (ADR 0008).

The session is a _NoDatabase: an invalid range, date, cursor or limit must be rejected without
touching the database, and the response must not echo the rejected value. Results are tested
against PostgreSQL in tests/integration/test_transactions_api.py.
"""

import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transaction_analytics.api.deps import get_owner_id, get_session
from transaction_analytics.main import create_app

MARKER = "LEAK-ME-3306"
OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
BASE = "/accounts/00000000-0000-4000-8000-0000000000aa"
PATHS = [f"{BASE}/transactions", f"{BASE}/summary", f"{BASE}/summary/monthly"]


class _NoDatabase:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"database used: {name}")


@pytest.fixture
def client() -> TestClient:
    app: FastAPI = create_app()
    app.dependency_overrides[get_session] = _NoDatabase
    app.dependency_overrides[get_owner_id] = lambda: OWNER_ID
    return TestClient(app)


@pytest.mark.parametrize("path", PATHS)
def test_from_after_to_is_rejected(client: TestClient, path: str) -> None:
    response = client.get(path, params={"from": "2026-09-02", "to": "2026-09-01"})

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_date_range", "message": "'from' must not be after 'to'."}
    }


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("param", ["from", "to"])
@pytest.mark.parametrize(
    "value",
    [
        MARKER,
        "2026-9-1",
        "20260901",
        "2026-02-30",  # the right shape, not a calendar date
        "1699920000",  # pydantic alone would read this as a Unix time
        "2026-09-01T00:00:00Z",  # and this as a date
        " 2026-09-01",
        "",
    ],
)
def test_malformed_date_is_rejected_without_echo(
    client: TestClient, caplog: pytest.LogCaptureFixture, path: str, param: str, value: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.get(path, params={param: value})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_request"
    assert [detail["loc"] for detail in body["details"]] == [["query", param]]
    assert MARKER not in response.text
    # The test client logs the URL it requested (like a server's access log would); everything
    # the app itself logs must be clean.
    app_logs = [record.getMessage() for record in caplog.records if record.name != "httpx2"]
    assert not any(MARKER in message for message in app_logs)


@pytest.mark.parametrize(
    "value",
    [
        "\u0662\u0660\u0662\u0666-\u0661\u0660-\u0661\u0665",  # Arabic-Indic digits
        "\uff12\uff10\uff12\uff16-\uff11\uff10-\uff11\uff15",  # full-width digits
    ],
)
def test_non_ascii_digits_are_refused_by_the_format_check(client: TestClient, value: str) -> None:
    # Review fix: \d let these through the YYYY-MM-DD check; only pydantic's parser stopped them
    # (type date_from_datetime_parsing). The format rule is the domain's, and it is ASCII only.
    response = client.get(f"{BASE}/summary", params={"from": value})

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"loc": ["query", "from"], "type": "value_error"}
    ]


@pytest.mark.parametrize("cursor", [MARKER, "A" * 64, "!!!!"])
def test_malformed_cursor_is_rejected_without_echo(client: TestClient, cursor: str) -> None:
    response = client.get(f"{BASE}/transactions", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalid_cursor", "message": "Cursor is invalid."}}


def test_overlong_cursor_is_rejected_before_decoding(client: TestClient) -> None:
    response = client.get(f"{BASE}/transactions", params={"cursor": MARKER * 100})

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"loc": ["query", "cursor"], "type": "string_too_long"}
    ]
    assert MARKER not in response.text


@pytest.mark.parametrize("limit", ["0", "-1", "501", "abc", MARKER])
def test_limit_out_of_bounds_is_rejected(client: TestClient, limit: str) -> None:
    response = client.get(f"{BASE}/transactions", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["query", "limit"]
    assert MARKER not in response.text


@pytest.mark.parametrize("path", PATHS)
def test_malformed_account_id_is_rejected_without_echo(client: TestClient, path: str) -> None:
    response = client.get(path.replace(BASE, f"/accounts/{MARKER}"))

    assert response.status_code == 422
    assert MARKER not in response.text


def test_openapi_names_the_range_parameters_from_and_to(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/accounts/{account_id}/summary"]["get"]

    assert [parameter["name"] for parameter in operation["parameters"]] == [
        "account_id",
        "from",
        "to",
    ]
    assert set(schema["components"]["schemas"]["SummaryOut"]["properties"]) == {
        "account_id",
        "from",
        "to",
        "totals",
    }
