# ADR 0005 — API conventions: error envelope, no echoed values, placeholder owner

- Status: Accepted
- Date: 2026-10-17

## Context

Phase 3 adds the first endpoints that read and write personal financial data. Error responses
and logs must never contain values from requests or files (README "Errors", ADR 0003 item 10),
and the defaults of the stack do not guarantee that:

- FastAPI's default 422 body includes `input`, the rejected value, and pydantic messages can
  quote parts of it (e.g. the offending character of a malformed UUID).
- Starlette's `ServerErrorMiddleware` re-raises every unhandled exception after responding, so
  the server (uvicorn) logs the full traceback, including `str(exc)`. For a client-side asyncpg
  `DataError` that text contains the value itself, even with `hide_parameters=True`.

Authentication arrives in Phase 5, but endpoints need an owner now.

## Decision

1. **One error envelope**: `{"error": {"code": "<stable code>", "message": "<fixed text>"}}`,
   plus `import_id` where it helps (409 duplicate, 500 with a recorded failed import) and
   `details` for 422. Codes are stable identifiers; messages are fixed text.
2. **422 returns `loc` and `type` only**, never `input`, `msg` or `ctx`. For an unknown JSON
   key (`extra_forbidden`) pydantic puts the client's key into `loc`; it is replaced with
   `"<extra>"`, because a key is client text like any value.
3. **`SafeErrorMiddleware`** sits inside `ServerErrorMiddleware`, turns any unhandled exception
   into a fixed 500 and does **not** re-raise. It logs the exception class (for a `DBAPIError`,
   the driver class and constraint name), the method, the path and the innermost frames as
   `file:line in function`; never the exception text or local values. If the response had
   already started, it only logs. A client that disconnects mid-request (`ClientDisconnect`) is
   logged at INFO and gets no response: nothing failed on our side.
   `tests/api/test_server_logs.py` runs a real uvicorn and checks its log stays clean.
4. **Another owner's resources are 404, not 403**, so IDs cannot be probed for existence.
   Ownership is enforced by the data layer: every function takes `owner_id` (keyword-only) or a
   row already loaded through it.
5. **Placeholder owner until Phase 5**: `get_owner_id` is a FastAPI dependency returning
   `DEV_OWNER_ID` from settings (fixed default). Not a header such as `X-Owner-Id`: that would
   make a spoofable value part of the API contract. Phase 5 replaces the dependency; routes and
   services do not change.
6. **Request bodies are capped at 64 KiB** (`RequestSizeLimitMiddleware`) except the CSV
   upload, which streams and has its own limit. FastAPI reads a JSON body fully before
   validating it, so `max_length` on a field does not bound memory. The middleware reads the
   body (bounded) and replays it, because FastAPI turns an exception raised while reading the
   body into a generic 400. 413 uses the same error envelope.
7. **HTTP status mapping lives in `api/errors.py`**; services raise `ServiceError` subclasses
   without HTTP knowledge. A unit test fails if a `ServiceError` subclass has no status.

## Consequences

- Until Phase 5, anyone who can reach the API acts as the one configured owner. The server must
  stay on localhost (`make run` binds 127.0.0.1).
- Debugging a 500 relies on the logged class name, constraint name and source location, not the
  message. This is deliberate; reproducing with synthetic data is the way to get details.
- A non-standard HTTP status (e.g. 499) keeps its code, with the generic message "HTTP error".
- Starlette lists only the first matching route's method in `Allow` for a 405 when one path has
  several routes (e.g. `GET` and `POST /accounts`). Cosmetic; not worked around.
