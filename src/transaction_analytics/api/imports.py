import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from transaction_analytics.api.deps import ImportLimitsDep, OwnerIdDep, SessionDep
from transaction_analytics.api.errors import PayloadTooLargeError, UnsupportedMediaTypeError
from transaction_analytics.api.schemas import ImportOut, RowErrorOut, RowErrorPageOut
from transaction_analytics.services import imports as import_service

router = APIRouter(tags=["imports"])

MAX_ERRORS_PAGE_SIZE = 500

# The body is read from the raw stream (ADR 0004), so OpenAPI is told about it explicitly.
_CSV_BODY = {
    "requestBody": {
        "required": True,
        "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
    }
}


async def _read_csv_body(request: Request, max_bytes: int) -> bytes:
    """The raw body, refused once it exceeds max_bytes, so an oversized upload is never held.

    The parser enforces the same limit (FILE_TOO_LARGE -> failed job) for callers other than the
    API. Through the API an oversized body is refused here first, with 413 and no job; both
    checks use the same ImportLimits.max_bytes.
    """
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "text/csv":
        raise UnsupportedMediaTypeError
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise PayloadTooLargeError(max_bytes)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        # Content-Length may be missing (chunked upload) or wrong, so the stream is counted too.
        if len(body) > max_bytes:
            raise PayloadTooLargeError(max_bytes)
    return bytes(body)


@router.post(
    "/accounts/{account_id}/imports",
    status_code=201,
    openapi_extra=_CSV_BODY,
    responses={
        200: {
            "description": "The same file was already rejected with the same error; "
            "that import is returned and no new one is created.",
            "model": ImportOut,
        }
    },
)
async def upload_statement(
    account_id: uuid.UUID,
    request: Request,
    response: Response,
    session: SessionDep,
    owner_id: OwnerIdDep,
    limits: ImportLimitsDep,
) -> ImportOut:
    """Import a CSV statement. A rejected file still creates an import, with status "failed"."""
    content = await _read_csv_body(request, limits.max_bytes)
    result = await import_service.import_statement(
        session, owner_id=owner_id, account_id=account_id, content=content, limits=limits
    )
    # url_for includes root_path, so the URL is right behind a proxy that mounts us under a prefix.
    url = str(request.url_for("get_import", import_id=str(result.job.id)))
    if result.created:
        response.headers["Location"] = url
    else:
        response.status_code = 200
        response.headers["Content-Location"] = url
    return ImportOut.of(result.job)


@router.get("/imports/{import_id}")
async def get_import(import_id: uuid.UUID, session: SessionDep, owner_id: OwnerIdDep) -> ImportOut:
    job = await import_service.get_import(session, owner_id=owner_id, import_id=import_id)
    return ImportOut.of(job)


@router.get("/imports/{import_id}/errors")
async def list_import_errors(
    import_id: uuid.UUID,
    session: SessionDep,
    owner_id: OwnerIdDep,
    after_row: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_ERRORS_PAGE_SIZE)] = 100,
) -> RowErrorPageOut:
    """Row errors ordered by row number. Row 1 is the header, so after_row=1 starts at the top."""
    page = await import_service.list_import_errors(
        session, owner_id=owner_id, import_id=import_id, after_row=after_row, limit=limit
    )
    return RowErrorPageOut(
        items=[RowErrorOut.of(error) for error in page.items],
        next_after_row=page.next_after_row,
    )
