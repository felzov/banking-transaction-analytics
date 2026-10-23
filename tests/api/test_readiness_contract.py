"""GET /ready and GET /health over HTTP, without a database (ADR 0010)."""

from collections.abc import Iterator
from typing import Any

import fastapi.routing
import pytest
from fastapi.testclient import TestClient

from transaction_analytics.api.deps import get_session
from transaction_analytics.data import schema as schema_data
from transaction_analytics.data.schema import EXPECTED_SCHEMA_REVISION
from transaction_analytics.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    # No get_owner_id override: the probes must work without a token.
    app = create_app()
    app.dependency_overrides[get_session] = lambda: object()
    yield TestClient(app)


def _schema_at(monkeypatch: pytest.MonkeyPatch, revisions: list[str]) -> None:
    async def schema_revisions(session: object) -> list[str]:
        return revisions

    monkeypatch.setattr(schema_data, "schema_revisions", schema_revisions)


def test_ready(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _schema_at(monkeypatch, [EXPECTED_SCHEMA_REVISION])

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


def test_not_ready_is_a_503_that_says_nothing_more(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _schema_at(monkeypatch, ["0001"])

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "not_ready", "message": "Service is not ready."}}
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers


def test_liveness_runs_on_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review fix (B1): a sync endpoint costs a threadpool hop per call and competes with the CSV
    # parser's worker threads (api/deps.py). Observed, not inspected.
    calls: list[str] = []
    real = fastapi.routing.run_in_threadpool

    async def recording(function: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(function, "__name__", repr(function)))
        return await real(function, *args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", recording)

    assert client.get("/health").status_code == 200
    assert calls == []


def test_liveness_does_not_touch_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(session: object) -> list[str]:
        raise ConnectionRefusedError

    monkeypatch.setattr(schema_data, "schema_revisions", unreachable)

    assert client.get("/ready").status_code == 503
    assert client.get("/health").json() == {"status": "ok"}
