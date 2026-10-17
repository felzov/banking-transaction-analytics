"""Request body size limit for everything except the CSV upload (ADR 0005).

FastAPI reads a JSON body completely before validating it, so without a cap one request could make
the server hold any amount of memory (`max_length` on a field only applies after the read). The
upload route streams its body and enforces its own, larger limit (ImportLimits.max_bytes), so it
is exempt here.

The limit cannot be raised from inside `receive`: FastAPI turns any exception while reading the
body into a generic 400. So the body is read here, bounded, and replayed to the app.
"""

import re

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from transaction_analytics.api.errors import error_response

MAX_JSON_BYTES = 64 * 1024

# The upload route in api/imports.py; tests/api/test_error_contract.py checks the two agree.
_UPLOAD_PATH = re.compile(r"/accounts/[^/]+/imports")


def is_upload_path(path: str) -> bool:
    return _UPLOAD_PATH.fullmatch(path) is not None


def _route_path(scope: Scope) -> str:
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path) :]
    return path


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_JSON_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_upload_path(_route_path(scope)):
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._refuse(scope, receive, send)
            return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                return  # http.disconnect: nobody is waiting for an answer
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
            # Content-Length may be missing (chunked) or wrong, so the stream is counted too.
            if len(body) > self.max_bytes:
                await self._refuse(scope, receive, send)
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            413, "payload_too_large", f"Request body is larger than {self.max_bytes} bytes."
        )
        await response(scope, receive, send)
