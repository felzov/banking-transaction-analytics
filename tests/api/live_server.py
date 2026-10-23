"""uvicorn in-process on a free local port, for tests about what the real server does."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI


@asynccontextmanager
async def live_server(app: FastAPI, **config: Any) -> AsyncIterator[str]:
    """Serves the app until the block ends; yields its base URL.

    log_config=None: uvicorn keeps its loggers propagating, so caplog sees what it would log.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, **config))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started or serving.done():
                break
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await serving
