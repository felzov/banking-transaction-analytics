# ADR 0004 — Import pipeline: synchronous, one transaction, partial import

- Status: Accepted; item 3 amended by ADR 0006 item 6 (duplicate_rows is set after the insert)
- Date: 2026-10-17

## Context

Phase 3 connects the parser (ADR 0002) to the schema (ADR 0003) through an HTTP upload. Open
questions left by those ADRs: whether many row errors should fail an import, how a file whose
import failed can be uploaded again, and how the service keeps the invariants the schema cannot
express (job counters equal stored rows; a CSV row is never both a transaction and an error; only
completed jobs own transactions). Files are at most 5 MiB / 50 000 rows.

## Decision

1. **Transport: the raw request body, `Content-Type: text/csv`.** Not multipart: that needs an
   extra dependency (`python-multipart`), and Starlette spools large multipart files to a
   temporary file on disk, which would put statements on the server's disk. The body is read as
   a stream and refused with 413 as soon as it exceeds the limit; `Content-Length` is checked
   first but not trusted. Other media types get 415.
2. **Synchronous, inside the request.** Order: check the account belongs to the owner (404
   otherwise) → hash (SHA-256 of the raw bytes) → parse in a worker thread (`asyncio.to_thread`;
   parsing is CPU-bound and would block the event loop) → one transaction.
3. **One transaction, job inserted in its final state.** Parsing happens before any write, so
   the outcome is known: the job is inserted directly as `completed` (with counters) or `failed`
   (with the file error), followed by its transactions and row errors (multi-row INSERTs), then
   one COMMIT. Job and rows commit or roll back together, which makes all three invariants above
   true by construction: the counters and the rows come from the same `ParseResult`, the parser
   puts each row in exactly one list, and failed jobs never get rows.
   `pending` and `processing` stay in the schema for a future asynchronous mode; nothing writes
   them now. `started_at` and `finished_at` are both `now()` (the database clock, ADR 0003),
   i.e. the start of the import transaction; they do not measure duration.
   *Amended by ADR 0006:* `duplicate_rows` is known only after the transactions are inserted, so
   it is set by an UPDATE in the same transaction. Nothing outside the transaction sees the job
   without it.
4. **Partial import, no threshold.** Valid rows are stored and every invalid row is reported,
   as ADR 0002 intends. A file whose rows are all invalid still completes, with `valid_rows = 0`,
   so the user sees each row error rather than one generic failure.
5. **A rejected file is still an import.** File errors (`empty_file`, `missing_columns`, ...)
   create a `failed` job with the file error code, returned with **201**. The import resource
   exists and its outcome is in the body. 4xx is reserved for requests that create nothing:
   413, 415, 404 (account), 409 (duplicate), 422 (malformed request).
6. **Duplicates and retries: a partial unique index.** Migration 0002 replaces
   `UNIQUE (owner_id, account_id, file_sha256)` with
   `uq_import_jobs_active_file_sha256 ... WHERE status <> 'failed'`. The same file cannot be
   imported twice into an account (409, with the id of the existing import), but a failed attempt
   does not block a retry, and failed attempts stay as history. One exception: a file that was
   already **rejected with the same error code** returns that failed import (**200**, with
   `Content-Location`) instead of adding another one. A rejection is deterministic for the same
   bytes and limits, so a retry loop would otherwise grow the table without bound; a different
   outcome (e.g. after a limit change) creates a new import. `internal_error` failures are never
   reused: they are transient by nature. Two identical rejected uploads that race can still both
   be stored; the bound is concurrency, not attempts. There is no SELECT before the
   INSERT: the index decides, inside a savepoint, which also handles two concurrent uploads of
   the same file (the second waits for the first to commit, then gets the violation).
7. **Unexpected database errors.** Everything is rolled back; a second, short transaction records
   a `failed` job with `internal_error`, and the client gets 500 with that import's id. Logs get
   only the driver exception class and constraint name (ADR 0003, item 10). If recording the
   failure fails too, the client still gets 500, without an id.
8. **Rows are inserted in batches per INSERT call** (1 000; 250 since ADR 0006 item 4, which
   inserts in key order). One call for 50 000 rows made
   SQLAlchemy prepare every parameter set in one step on the event loop: measured stall 0.42 s for
   all concurrent requests. With batches the longest stall measured is 0.02 s; total time is
   unchanged (~3.8 s for 50 000 rows, local).
9. **`Location` is absolute and includes `root_path`**, so it stays correct behind a proxy that
   mounts the API under a prefix.
10. **The service owns the transaction.** `import_statement` rolls back the read transaction of
   the account check (so no pooled connection is held during the parse) and commits the import;
   callers pass a session with no pending work.
11. **Limits are settings** (`IMPORT_MAX_BYTES`, `IMPORT_MAX_ROWS`), defaulting to the parser's
   defaults (5 MiB, 50 000 rows).

## Consequences

- ~~Re-importing a corrected file duplicates its valid rows~~ and ~~the same statement with and
  without a UTF-8 BOM is not a duplicate~~: resolved by row-level deduplication (ADR 0006).
- The client waits for the whole import: about 4 s for 50 000 rows locally (no connection is held
  during the parse, only during the insert). If real files are slower, move to an asynchronous
  job (the `pending`/`processing` states exist for that).
- The whole file is in memory twice at most (body + decoded text), bounded by the 5 MiB limit.
- Downgrading migration 0002 fails once a file has been retried after a failure (two rows with
  the same hash); acceptable for a development-stage schema.

## Alternatives considered

- `BackgroundTasks` / a queue: a crash between "accepted" and "done" leaves a job stuck in
  `processing` unless a reaper exists. Not needed at this size.
- All-or-nothing import: avoids the corrected-file duplicates, but contradicts ADR 0002 (one bad
  line must not sink the import) and needs a new failure code.
- Reusing the failed job (UPDATE in place): no schema change, but overwrites history and adds
  state transitions to a row that is otherwise written once.
- 422 for rejected files: mixes "the request is malformed" with "the file was processed and
  rejected", and would leave the failed import without a resource to point to.
