"""HTTP error contract (ADR 0005).

Every error body is {"error": {"code": ..., "message": ...}} with fixed text. Nothing from the
request or the uploaded file is echoed, because statements are personal financial data:

- Validation errors report where and what kind (loc, type), never the rejected input.
- Unexpected exceptions are caught by SafeErrorMiddleware and NOT re-raised. Starlette's
  ServerErrorMiddleware re-raises every exception so the server logs the full traceback, and a
  DBAPIError's text can contain row values (ADR 0003, item 10). The log gets the class name and
  where it happened (file, line, function), never the exception's message.
"""

import logging
import traceback
from http import HTTPStatus
from pathlib import PurePath
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from transaction_analytics.data.errors import constraint_name, error_kind
from transaction_analytics.domain.errors import AccountValidationError
from transaction_analytics.services.errors import (
    AccountNotFoundError,
    DuplicateImportError,
    ImportNotFoundError,
    ImportProcessingError,
    ServiceError,
)

logger = logging.getLogger(__name__)

# Innermost frames logged for an unhandled exception: enough to find the failing line.
_LOGGED_FRAMES = 5

SERVICE_ERROR_STATUS: dict[type[ServiceError], int] = {
    AccountNotFoundError: 404,
    ImportNotFoundError: 404,
    DuplicateImportError: 409,
    ImportProcessingError: 500,
}


class ApiError(Exception):
    """A request the API refuses before any service is called."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class PayloadTooLargeError(ApiError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(413, "payload_too_large", f"File is larger than {max_bytes} bytes.")


class UnsupportedMediaTypeError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            415,
            "unsupported_media_type",
            "Send the file as the raw request body with Content-Type: text/csv.",
        )


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, **extra}},
        status_code=status_code,
        headers=headers,
    )


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    extra: dict[str, str] = {}
    if isinstance(exc, DuplicateImportError):
        extra["import_id"] = str(exc.existing_import_id)
    elif isinstance(exc, ImportProcessingError) and exc.failed_import_id is not None:
        extra["import_id"] = str(exc.failed_import_id)
    return error_response(SERVICE_ERROR_STATUS.get(type(exc), 500), exc.code, exc.message, **extra)


async def _account_validation_error(request: Request, exc: AccountValidationError) -> JSONResponse:
    return error_response(422, exc.code, exc.message)


async def _request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default body includes "input" (the rejected value) and pydantic's "msg" can quote
    # parts of it; only location and error type are returned.
    details = [{"loc": _safe_loc(error), "type": error["type"]} for error in exc.errors()]
    return error_response(422, "invalid_request", "Request is invalid.", details=details)


def _safe_loc(error: Any) -> list[str | int]:
    # loc holds our field names and positions, except for an unknown key: pydantic reports the
    # client's own key there, and a key is client text like any value.
    loc = list(error["loc"])
    if error["type"] == "extra_forbidden":
        loc[-1] = "<extra>"
    return loc


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:  # a status code outside the standard list, e.g. 499
        message = "HTTP error"
    return error_response(exc.status_code, code, message, headers=exc.headers)


def _where(exc: BaseException) -> str:
    """Innermost frames as "file:line in function". Source locations only, no exception text."""
    frames = traceback.extract_tb(exc.__traceback__)[-_LOGGED_FRAMES:]
    return " <- ".join(
        f"{PurePath(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in reversed(frames)
    )


class SafeErrorMiddleware:
    """Turns any unhandled exception into a fixed 500 without passing it to the server."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ClientDisconnect:
            # The client went away mid-upload: nobody to answer, nothing broken on our side.
            logger.info("Client disconnected during %s %s", scope["method"], scope["path"])
        except Exception as exc:
            if isinstance(exc, DBAPIError):
                kind = f"{error_kind(exc)} (constraint: {constraint_name(exc)})"
            else:
                kind = type(exc).__name__
            logger.error(
                "Unhandled %s in %s %s at %s", kind, scope["method"], scope["path"], _where(exc)
            )
            if not response_started:
                response = error_response(500, "internal_error", "Internal server error.")
                await response(scope, receive, send)


def install_error_handling(app: FastAPI) -> None:
    app.add_middleware(SafeErrorMiddleware)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(AccountValidationError, _account_validation_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(HTTPException, _http_error)
