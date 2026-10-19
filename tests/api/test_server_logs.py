"""The real server's error log, end to end: uvicorn in-process on a free local port.

uvicorn logs "Exception in ASGI application" with the full traceback (including str(exc)) for
any exception that escapes the app. A DBAPIError's text can carry row values (ADR 0003, item 10),
so nothing may escape: SafeErrorMiddleware must turn it into a 500 first (ADR 0005).
"""

import asyncio
import logging
import socket

import httpx2
import pytest
import uvicorn
from sqlalchemy.exc import DBAPIError

from transaction_analytics.main import create_app

MARKER = "LEAK-ME-3456"


async def test_unhandled_database_error_never_reaches_the_server_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise DBAPIError("INSERT ...", {"description": MARKER}, Exception(f"value {MARKER}"))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    # log_config=None: uvicorn keeps its loggers propagating, so caplog sees what it would log.
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None))

    with caplog.at_level(logging.DEBUG):
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(500):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            port = sock.getsockname()[1]
            async with httpx2.AsyncClient() as client:
                response = await client.get(f"http://127.0.0.1:{port}/boom")
        finally:
            server.should_exit = True
            await serving

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "Exception in ASGI application" not in caplog.text
    assert MARKER not in caplog.text
