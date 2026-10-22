# Banking Transaction Analytics

[![CI](https://github.com/felzov/banking-transaction-analytics/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/felzov/banking-transaction-analytics/actions/workflows/ci.yml)

Import, validate, normalize, deduplicate and analyze bank-statement transactions.

**Status: early development (Phase 7 of 8).** What exists today:

- `/health` liveness endpoint (FastAPI).
- A pure, fully unit-tested domain layer: a CSV statement parser that returns valid transactions
  plus row-level errors, with explicit money, date and description rules.
- PostgreSQL schema (SQLAlchemy models + Alembic migrations) for accounts, import jobs,
  transactions and row errors, with ownership and data rules enforced by constraints
  ([ADR 0003](docs/adr/0003-persistence-schema.md)).
- An HTTP API to create accounts, upload a statement CSV and read the import result and its
  row errors ([ADR 0004](docs/adr/0004-import-pipeline.md),
  [ADR 0005](docs/adr/0005-api-conventions.md)).
- Deduplication on re-import: rows an account already holds are skipped and counted, so corrected,
  overlapping or re-saved statements can be uploaded safely
  ([ADR 0006](docs/adr/0006-deduplication.md)).
- Authentication with personal API tokens (`Authorization: Bearer ...`), issued and revoked with an
  operator CLI. Every owner is a user; another user's data is invisible
  ([ADR 0007](docs/adr/0007-authentication.md)).
- Reading an account back: its transactions (newest first, paged), and inflow, outflow and net
  per currency, overall or per month ([ADR 0008](docs/adr/0008-analytics.md)).
- A second source: accounts linked to a [banking-ledger-api](https://github.com/felzov/banking-ledger-api)
  account, synced on request with the caller's ledger token (never stored), checked entry by entry
  and deduplicated by the ledger's entry id ([ADR 0009](docs/adr/0009-ledger-source.md)).

Not built yet: totals across accounts, categories or merchant grouping, export, self-service
sign-up or password login.
The target users (accountants, bookkeepers, freelancers, small businesses) are hypotheses that
have **not** been validated.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
make install   # uv sync
make check     # ruff lint + format check + unit tests (no database needed)
make run       # http://127.0.0.1:8000/health and /docs
```

## Database

Requires Docker. PostgreSQL 17 runs via `compose.yaml`, bound to `127.0.0.1:5433`.

```bash
cp .env.example .env   # then set a local-only password in POSTGRES_PASSWORD and both URLs
make db-up             # start Postgres and wait until it is healthy
make db-migrate        # alembic upgrade head on DATABASE_URL
make db-down           # stop it (data stays in the db-data volume)
```

- `DATABASE_URL` (app and Alembic) and `TEST_DATABASE_URL` come from the environment or `.env`;
  no URL or password is stored in code or `alembic.ini`.
- New migration: `make db-revision m="describe the change"`, then write `upgrade()` and
  `downgrade()` by hand. Allowed values in CHECK constraints are literals, not imports
  ([ADR 0003](docs/adr/0003-persistence-schema.md)).
- Print the SQL without running it: `uv run alembic upgrade head --sql`.

## Authentication

Every endpoint except `/health` needs an API token ([ADR 0007](docs/adr/0007-authentication.md)).
Users and tokens are managed with the CLI, which uses `DATABASE_URL` like the app:

```bash
uv run python -m transaction_analytics.cli create-user --name "Max"        # prints the user id
uv run python -m transaction_analytics.cli issue-token --user-id <user_id> --name laptop
# prints the token once (bta_...); store it now, it cannot be shown again. --days 1-365, default 90
uv run python -m transaction_analytics.cli list-tokens --user-id <user_id>  # id, state, expiry, name
uv run python -m transaction_analytics.cli revoke-token --user-id <user_id> --token-id <token_id>
uv run python -m transaction_analytics.cli disable-user --user-id <user_id> # refuses all its tokens
```

`make cli args="create-user --name Max"` does the same. Only the SHA-256 of a token is stored.
Data imported before Phase 5 belongs to the old placeholder owner, which migration 0004 turned into
a user: `issue-token --user-id 00000000-0000-4000-8000-000000000001` (the former `DEV_OWNER_ID`
default) gets it back.

A missing, malformed, unknown, expired or revoked token, or a disabled user, all get the same
`401 {"error": {"code": "not_authenticated", ...}}`. `WWW-Authenticate` is `Bearer` without a token
and `Bearer error="invalid_token"` when one was sent (RFC 6750). A disabled user gets no new tokens.

## API

Start the database and the app (`make db-up && make db-migrate && make run`), get a token (above),
then:

```bash
export TOKEN=bta_...   # from issue-token
AUTH="Authorization: Bearer $TOKEN"

curl -s localhost:8000/me -H "$AUTH"
# -> {"id": "<user_id>", "name": "Max", "created_at": "..."}

# Create an account
curl -s -X POST localhost:8000/accounts -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Current account"}'
# -> {"id": "<account_id>", "name": "Current account", "source": "csv", "ledger": null,
#     "created_at": "..."}

# Upload a statement: the raw file as the body, Content-Type text/csv (not multipart)
curl -s -X POST localhost:8000/accounts/<account_id>/imports -H "$AUTH" \
  -H 'Content-Type: text/csv' --data-binary @tests/fixtures/statement_mixed_errors.csv
# -> 201 {"id": "<import_id>", "status": "completed", "total_rows": 8, "valid_rows": 2,
#         "invalid_rows": 6, "duplicate_rows": 0, "imported_rows": 2, "error": null, ...}

curl -s localhost:8000/imports/<import_id> -H "$AUTH"
curl -s 'localhost:8000/imports/<import_id>/errors?limit=100' -H "$AUTH"   # page with after_row=

# Transactions, newest first; pass next_cursor back as cursor= for the next page
curl -s 'localhost:8000/accounts/<account_id>/transactions?from=2026-09-01&to=2026-09-30&limit=100' \
  -H "$AUTH"
# -> {"items": [{"id": "...", "source": "csv", "import_id": "...", "ledger_entry_id": null,
#                "booked_on": "2026-09-07", "description": "Refund",
#                "currency": "GBP", "amount_minor": 1500, "amount": "15.00"}, ...],
#     "next_cursor": null}

# Totals per currency (from/to optional, both inclusive); /summary/monthly adds "month": "2026-09"
curl -s 'localhost:8000/accounts/<account_id>/summary?from=2026-09-01&to=2026-09-30' -H "$AUTH"
# -> {"account_id": "...", "from": "2026-09-01", "to": "2026-09-30",
#     "totals": [{"currency": "GBP", "transactions": 2,
#                 "inflow_minor": 1500, "inflow": "15.00",
#                 "outflow_minor": -95000, "outflow": "-950.00",
#                 "net_minor": -93500, "net": "-935.00"}]}
```

Interactive docs: http://127.0.0.1:8000/docs.

| Endpoint | Success | Errors |
|---|---|---|
| `GET /me` | 200 | 401 |
| `POST /accounts` | 201 | 401, 409 ledger account already linked, 413 (body over 64 KiB), 422 |
| `GET /accounts`, `GET /accounts/{id}` | 200 | 401, 404 |
| `POST /accounts/{id}/imports` | 201 (also when the file is rejected: `status: "failed"`); 200 when the same file was already rejected with the same error | 401, 404, 409 duplicate or ledger account, 413, 415, 500 |
| `POST /accounts/{id}/ledger-syncs` | 201 with new entries; 204 when there were none | 401, 404, 409 CSV account, 422, 502, 503 (see [Ledger accounts](#ledger-accounts-adr-0009)) |
| `GET /ledger-syncs/{id}` | 200 | 401, 404 |
| `GET /imports/{id}` | 200 | 401, 404 |
| `GET /imports/{id}/errors?after_row=&limit=` | 200 | 401, 404, 422 |
| `GET /accounts/{id}/transactions?from=&to=&limit=&cursor=` | 200 | 401, 404, 422 |
| `GET /accounts/{id}/summary?from=&to=`, `GET /accounts/{id}/summary/monthly?from=&to=` | 200 | 401, 404, 422 |
| `GET /health` | 200 | — (no token needed) |

An upload without a valid token gets 401 before its body is read. A JSON body over 64 KiB gets 413
before authentication (the size limit sits in front of the app).

- The import runs synchronously in the request. Valid rows are stored and every invalid row is
  reported; there is no error threshold.
- Rows the account already holds are not stored again (ADR 0006). A row is the same when date,
  amount, currency and description match (description ignoring case and extra spaces). Identical
  rows inside one file are kept: if a file has a row 3 times and the account holds it twice, one
  is added. `duplicate_rows` counts the skipped rows; `imported_rows = valid_rows - duplicate_rows`
  were stored.
- The same file (same bytes) cannot be imported twice into one account: 409 with the existing
  `import_id`. A file whose import **failed** can be uploaded again; if it is rejected again
  with the same error, the earlier failed import is returned (200) instead of a new one.
- Errors are `{"error": {"code", "message"}}` with fixed text, never values from the request or
  the file. Another owner's account or import is 404.
- There is no TLS and no rate limiting yet (Phase 8): keep the server on localhost or behind a proxy
  that provides both.

### Amounts, totals and paging (ADR 0008)

- Every amount comes twice: `<name>_minor`, a JSON integer in the currency's minor unit (pence,
  cents; yen have none), and `<name>`, the same value as an exact decimal string. Use the string
  if your JSON parser turns large integers into floats (JavaScript does above 2^53). Negative =
  money out.
- Totals are per currency and never added across currencies: `inflow` sums the positive amounts,
  `outflow` the negative ones (it stays negative), `net = inflow + outflow`. A zero amount is
  counted in `transactions` only. No transactions in the range means `"totals": []`, not zeros.
- `from` and `to` are `YYYY-MM-DD`, both inclusive, both optional; `from` after `to` is 422
  `invalid_date_range`. `/summary/monthly` leaves out months without transactions.
- Listing pages hold up to `limit` (1–500, default 100) transactions; `next_cursor` is `null` on
  the last page. A cursor is opaque; a changed one is 422 `invalid_cursor`. Transactions of one day
  are in a stable but arbitrary order.
- Everything is per account; there are no totals across an owner's accounts (transfers between
  them would count twice).

### Ledger accounts (ADR 0009)

An account takes its transactions either from CSV uploads or from one
[banking-ledger-api](https://github.com/felzov/banking-ledger-api) account, chosen when it is created.
The server needs `LEDGER_API_URL` (e.g. `http://127.0.0.1:8001`; unset = syncs answer 503).

```bash
# Link: the ledger's own user id and account id
curl -s -X POST localhost:8000/accounts -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Ledger GBP", "ledger": {"user_id": "<ledger_user_id>", "account_id": "<ledger_account_id>"}}'
# -> {"id": "<account_id>", "source": "ledger", "ledger": {"user_id": "...", "account_id": "..."}, ...}

# Sync: log in to the ledger (POST /sessions there) and pass its 30-minute token
curl -si -X POST localhost:8000/accounts/<account_id>/ledger-syncs -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"ledger_token": "lt_..."}'
# -> 201, Location: http://localhost:8000/ledger-syncs/<sync_id>
#    {"id": "<sync_id>", "account_id": "...", "fetched_entries": 5, "duplicate_entries": 0,
#     "imported_entries": 5, "created_at": "..."}
# The same again with nothing new in the ledger -> 204, no body, nothing recorded
```

- The ledger token is used for that sync only: never stored, logged or returned.
- Each ledger entry becomes one transaction: booked on the **UTC date** of its posting time,
  described as `Ledger deposit`, `Ledger withdrawal`, `Ledger transfer in` or `out`, in the ledger
  account's currency, with its `ledger_entry_id`. Totals of a ledger account equal its ledger
  balance.
- Re-syncing is safe: entries are matched by the ledger's entry id. A sync reads newest first and
  stops at the first page with an entry the account already has, so a sync without news reads one
  page and answers 204 without recording a sync. When there is news, `duplicate_entries` counts
  the stored entries it read again.
- Only the ledger's currencies (GBP, EUR) are accepted, because ledger amounts are taken over in
  their minor units as they are; a posting time whose UTC date falls outside years 1–9999 is an
  invalid response.
- Nothing is stored unless the whole statement checks out: shapes, signs, and the running balance
  from entry to entry (a history read to its start must begin at zero).
- A ledger account takes no CSV uploads and a CSV account no syncs (409 `account_source_mismatch`).
  One owner can link a ledger account once (409 `ledger_account_already_linked`).

| Sync error | Status | When |
|---|---|---|
| `invalid_ledger_token` | 422 | Not a ledger token (`lt_` + 43 characters); checked before anything else |
| `ledger_token_rejected` | 422 | The ledger answered 401: log in to the ledger again |
| `ledger_account_unavailable` | 422 | The ledger answered 403/404: the link names another ledger user, or a missing account |
| `ledger_too_many_entries` | 422 | The sync would read more than `LEDGER_SYNC_MAX_ENTRIES` entries (the new ones, plus the stored ones on its last page) |
| `ledger_unavailable` | 502 | Unreachable, timed out, 429 or 5xx |
| `ledger_invalid_response` | 502 | Malformed or inconsistent statement; nothing stored |
| `ledger_not_configured` | 503 | `LEDGER_API_URL` is not set |

| Setting | Default | |
|---|---|---|
| `LEDGER_API_URL` | unset | Base URL of banking-ledger-api, http(s), no `user:password@`. The only place a ledger call's destination comes from |
| `LEDGER_TIMEOUT_SECONDS` | 10 | Per ledger call |
| `LEDGER_SYNC_TIMEOUT_SECONDS` | 120 | One deadline for all ledger calls of a sync |
| `LEDGER_SYNC_MAX_ENTRIES` | 50000 | Entries one sync may read |

## Tests

| Command | Runs | Needs |
|---|---|---|
| `make test` | unit tests and database-free API tests (`-m "not integration"`) | nothing |
| `make test-integration` | constraints, migrations, round-trips, the API end to end (`-m integration`) | `make db-up`, `TEST_DATABASE_URL` |
| `make check` | lint + `make test` | nothing |
| `make check-all` | `make check` + `make test-integration` | as above |

Integration tests **drop and recreate** the database named in `TEST_DATABASE_URL` on every run
(plus a temporary `*_migrations_test` database). They refuse to start unless its name ends in
`_test`, and they fail rather than skip when the variable is missing. CI runs both sets on
Python 3.12 and 3.14 against a `postgres:17` service.

- Use the make targets. A bare `pytest` runs the integration tests too and errors without a
  database.
- Do not run integration tests concurrently from two checkouts (e.g. git worktrees) against the
  same server: each run force-drops the shared test databases, killing the other run.
- Run `alembic` and `make` from the repository root: `.env` is read from the current directory.
- The database must be UTF8 (the length limits count characters).
- Deprecation warnings fail the tests: every `DeprecationWarning` (Python's, Pydantic's,
  SQLAlchemy's) and Starlette's, FastAPI's and Uvicorn's own classes (`filterwarnings` in
  `pyproject.toml`). One raised while a test module is imported stops the whole run at
  collection, not just one test. Fix the code, or pin the dependency, when an upgrade starts one.

## Supported CSV format (v1)

| Rule | Behaviour |
|---|---|
| Encoding | UTF-8; a leading BOM (Excel "CSV UTF-8") is accepted. Anything else rejects the file. |
| Header | Required. Columns `date`, `description`, `amount`, `currency` in any order, case-insensitive. Extra columns are ignored. Duplicate names reject the file. |
| `date` | `YYYY-MM-DD` only, must be a real calendar date. |
| `amount` | Plain signed decimal: `-12.30`, `2500`. **Negative = money out (debit), positive = money in (credit).** No currency symbols, thousands separators or exponent notation. More decimal places than the currency allows is an error; nothing is rounded. |
| `currency` | ISO 4217 code: GBP, EUR, USD, JPY (case-insensitive). |
| `description` | Required, max 500 characters after whitespace is collapsed. Control characters (e.g. NUL) are a row error (`invalid_characters`); emoji and RTL marks are kept. |
| Blank lines | Skipped. |
| Limits | 5 MiB per file (larger uploads get 413), 50 000 data rows. Settings `IMPORT_MAX_BYTES`, `IMPORT_MAX_ROWS`. |

Row numbers in errors count the header as row 1, like a spreadsheet. A quoted value that spans
several lines still counts as one row.

Example:

```csv
date,description,amount,currency
2026-09-01,SALARY ACME LTD,2500.00,GBP
2026-09-02,Coffee Shop,-3.20,GBP
```

### Errors

- **Row errors** (`invalid_date`, `amount_too_precise`, `unsupported_currency`, ...) do not stop
  the import. Every valid row is still returned.
- **File errors** (`empty_file`, `invalid_encoding`, `missing_columns`, `malformed_csv`,
  `too_many_rows`, ...) reject the whole file.
- Error messages never contain values from the file, because statements contain personal
  financial data and errors end up in responses and logs.

Codes are defined in `src/transaction_analytics/domain/errors.py`.

## Project layout

```text
src/transaction_analytics/
  domain/      pure logic (no I/O): money, field rules, CSV parser, dedup keys, names, API token
               format, import outcome, analytics values (date range, cursor, totals), ledger
               entries and statement checks
  data/        SQLAlchemy models, engine/session factories, owner-scoped queries, DB error helpers
  integrations/ HTTP client for banking-ledger-api (outbound I/O, no business rules)
  services/    use cases and transaction boundaries (accounts, imports, ledger syncs, users, tokens)
  api/         FastAPI routers (thin), dependencies (incl. authentication), schemas, error handling
  cli.py       operator commands for users and API tokens (thin, like api/)
  settings.py  configuration from environment / .env
  main.py      app factory
migrations/    Alembic environment and versions
tests/
  unit/        domain and settings tests, schema guards (no DB)
  api/         HTTP tests that need no database (error contract, upload checks)
  integration/ tests against a real PostgreSQL, including the API end to end (marker: integration)
  fixtures/    anonymized sample statements only
docs/adr/      architecture decision records
```

## Known limitations

- One CSV dialect. Real bank exports (UK `DD/MM/YYYY` dates, separate debit/credit columns,
  `1,234.56` amounts) need explicit format adapters; none exist yet.
- Deduplication is exact (ADR 0006). If a correction changes a row that was already valid (a typo
  in the amount or description), the old row stays and the corrected one is added. If one day is
  split across two exports and each part has a different but identical-looking transaction, the
  second is skipped as a duplicate.
- Duplicates stored before Phase 4 (migration 0003) stay.
- The parser holds the whole file in memory (bounded by the 5 MiB limit).
- Authentication is API tokens only, managed by an operator with shell and database access: no
  sign-up, no password login, no token endpoints. No rate limiting of failed attempts and no
  "last used" time (ADR 0007).
- Imports are synchronous: the client waits while the file is parsed and stored (about 4 s for
  50 000 rows in the integration test); a database connection is held only for the insert. `started_at` and `finished_at` are
  the same instant (the import transaction's start), not a duration.
- The schema cannot prevent one CSV row being stored both as a transaction and as a row error,
  or check that job counters match the stored rows. The import service guarantees both by
  writing a job and its rows in one transaction (ADR 0004); integration tests check it.
- PostgreSQL's server log records CHECK violations with `DETAIL: Failing row contains (...)`,
  i.e. raw row values. Treat database log access as access to personal data. The app itself
  never logs or returns raw database exceptions (ADR 0003).
- Exporting descriptions back to CSV/Excel would need protection against formula injection
  (`=`, `+`, `-`, `@` prefixes). Not handled because there is no export yet.
- Totals inherit deduplication's limits (ADR 0006): a corrected valid row that stayed twice counts
  twice.
- Summaries of a whole account read every row of it: ~25 ms for 50 000 rows locally (ADR 0008).
- Server access logs contain query strings: date ranges and cursors (a booking date and a
  transaction id), never amounts or descriptions.
- Ledger syncs are synchronous and read at most `LEDGER_SYNC_MAX_ENTRIES` entries; a longer
  history cannot be synced the first time (all or nothing, newest first). Failed syncs, and syncs
  that found nothing new, are not recorded. Ledger entries are booked on their UTC date
  (ADR 0009).

## Decisions

- [ADR 0001 — Money: signed minor units, no rounding](docs/adr/0001-money-signed-minor-units.md)
- [ADR 0002 — CSV parsing: strict format, row-level errors](docs/adr/0002-csv-parsing-policy.md)
- [ADR 0003 — Persistence: ownership in the schema, CHECK over ENUM, isolated test databases](docs/adr/0003-persistence-schema.md)
- [ADR 0004 — Import pipeline: synchronous, one transaction, partial import](docs/adr/0004-import-pipeline.md)
- [ADR 0005 — API conventions: error envelope, no echoed values, placeholder owner](docs/adr/0005-api-conventions.md)
- [ADR 0006 — Deduplication: exact keys, multiset union, enforced by a unique constraint](docs/adr/0006-deduplication.md)
- [ADR 0007 — Authentication: opaque API tokens, stored hashed, issued by a CLI](docs/adr/0007-authentication.md)
- [ADR 0008 — Analytics: per-account, per-currency SQL totals; money in API responses](docs/adr/0008-analytics.md)
- [ADR 0009 — Ledger source: accounts fed by banking-ledger-api, pulled per request](docs/adr/0009-ledger-source.md)
