"""The rate limit over HTTP, with the real lifespan and no database (ADR 0010).

GET /me without a token is 401 without a query, so it shows whether a request got through.
"""

import time
from collections.abc import Iterator

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from live_server import live_server

from transaction_analytics.api.limits import MAX_JSON_BYTES
from transaction_analytics.api.rate_limit import TokenBucket
from transaction_analytics.main import create_app

RATE_LIMITED = {"error": {"code": "rate_limited", "message": "Too many requests. Retry later."}}


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app_environment: pytest.MonkeyPatch, app: FastAPI) -> Iterator[TestClient]:
    """Two requests at once, then one a minute: no pause of a slow runner refills the bucket."""
    app_environment.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app_environment.setenv("RATE_LIMIT_BURST", "2")
    with TestClient(app) as client:  # runs the lifespan, which builds the limiter
        yield client


def test_the_lifespan_builds_the_limiter_from_settings(app: FastAPI, client: TestClient) -> None:
    assert isinstance(app.state.rate_limiter, TokenBucket)


def test_requests_over_the_limit_get_429_with_retry_after(client: TestClient) -> None:
    statuses = [client.get("/me").status_code for _ in range(2)]
    refused = client.get("/me")

    assert statuses == [401, 401]
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED
    assert refused.headers["retry-after"] == "60"


@pytest.mark.parametrize(("probe", "answer"), [("/health", 200), ("/ready", 503)])
def test_the_probes_count_against_the_limit(client: TestClient, probe: str, answer: int) -> None:
    # Review fix (B1): exempt probes let one address send unlimited requests, each /ready with
    # database queries (2 400 in 4 s in a local test). (/ready is 503 here: there is no database.)
    # A deploy script or monitor stays well within the burst.
    statuses = [client.get(probe).status_code for _ in range(3)]

    assert statuses == [answer, answer, 429]


def test_probes_and_other_routes_share_one_budget(client: TestClient) -> None:
    client.get("/health")
    client.get("/ready")

    assert client.get("/me").status_code == 429


def test_a_slow_runner_still_sees_the_limit(client: TestClient) -> None:
    # Review fix (m2): at one token per second, a stall of a second between requests (a busy CI
    # runner) let the third request through and the 429 assertions failed at random.
    for _ in range(2):
        client.get("/me")

    time.sleep(1.1)

    assert client.get("/me").status_code == 429


def test_an_oversized_body_is_refused_before_it_is_read(client: TestClient) -> None:
    for _ in range(2):
        client.get("/me")

    response = client.post("/accounts", json={"name": "x" * (MAX_JSON_BYTES + 1)})

    assert response.status_code == 429  # not 413: the body was never read


def test_each_client_address_has_its_own_limit(app: FastAPI, client: TestClient) -> None:
    for _ in range(2):
        client.get("/me")
    other = TestClient(app, client=("203.0.113.9", 50000))  # same app and limiter

    assert client.get("/me").status_code == 429
    assert other.get("/me").status_code == 401


def test_zero_turns_the_limit_off(app_environment: pytest.MonkeyPatch, app: FastAPI) -> None:
    app_environment.setenv("RATE_LIMIT_PER_MINUTE", "0")

    with TestClient(app) as client:
        statuses = {client.get("/me").status_code for _ in range(50)}

    assert app.state.rate_limiter is None
    assert statuses == {401}


# --- behind a proxy: the real server's X-Forwarded-For handling ---------------------------------


async def _statuses(url: str, forwarded_for: list[str]) -> list[int]:
    async with httpx2.AsyncClient() as client:
        return [
            (await client.get(f"{url}/me", headers={"X-Forwarded-For": address})).status_code
            for address in forwarded_for
        ]


async def test_x_forwarded_for_from_an_untrusted_peer_is_ignored(
    app_environment: pytest.MonkeyPatch,
) -> None:
    # Otherwise any client could name a new address on every request and never be limited.
    app_environment.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app_environment.setenv("RATE_LIMIT_BURST", "1")

    async with live_server(create_app(), forwarded_allow_ips="192.0.2.1") as url:
        statuses = await _statuses(url, ["203.0.113.1", "203.0.113.2"])

    assert statuses == [401, 429]


async def test_a_trusted_proxy_passes_on_each_client_address(
    app_environment: pytest.MonkeyPatch,
) -> None:
    # Behind a proxy every request comes from the proxy; without this all clients share one limit.
    app_environment.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app_environment.setenv("RATE_LIMIT_BURST", "1")

    async with live_server(create_app(), forwarded_allow_ips="127.0.0.1") as url:
        statuses = await _statuses(url, ["203.0.113.1", "203.0.113.2", "203.0.113.1"])

    assert statuses == [401, 401, 429]
