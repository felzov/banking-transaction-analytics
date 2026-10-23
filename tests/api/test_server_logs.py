"""The real server's log, end to end: uvicorn in-process on a free local port.

uvicorn logs "Exception in ASGI application" with the full traceback (including str(exc)) for
any exception that escapes the app. A DBAPIError's text can carry row values (ADR 0003, item 10),
so nothing may escape: SafeErrorMiddleware must turn it into a 500 first (ADR 0005).
"""

import logging

import httpx2
import pytest
from fastapi import FastAPI
from live_server import live_server
from sqlalchemy.exc import DBAPIError

from transaction_analytics.main import create_app

MARKER = "LEAK-ME-3456"


def _app_with_a_database_error() -> FastAPI:
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise DBAPIError("INSERT ...", {"description": MARKER}, Exception(f"value {MARKER}"))

    return app


async def test_unhandled_database_error_never_reaches_the_server_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        async with (
            live_server(_app_with_a_database_error(), lifespan="off") as url,
            httpx2.AsyncClient() as client,
        ):
            response = await client.get(f"{url}/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "Exception in ASGI application" not in caplog.text
    assert MARKER not in caplog.text


async def test_app_errors_are_logged_with_time_level_and_logger(
    app_environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The lifespan configures the app's loggers (ADR 0010). Without it, uvicorn's log config left
    # them to Python's last-resort handler: a bare message on stderr, INFO dropped.
    async with (
        live_server(_app_with_a_database_error(), lifespan="on") as url,
        httpx2.AsyncClient() as client,
    ):
        response = await client.get(f"{url}/boom")

    captured = capsys.readouterr()
    assert response.status_code == 500
    (line,) = [line for line in captured.out.splitlines() if "Unhandled" in line]
    assert " ERROR transaction_analytics.api.errors Unhandled DBAPIError" in line
    assert MARKER not in captured.out + captured.err
