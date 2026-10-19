# Banking Transaction Analytics

[![CI](https://github.com/felzov/banking-transaction-analytics/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/felzov/banking-transaction-analytics/actions/workflows/ci.yml)

Import, validate, normalize, deduplicate and analyze bank-statement transactions.

**Status: early development (Phase 5 of 8).** What exists today:

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

Not built yet: analytics, listing transactions, self-service sign-up or password login.
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
# -> {"id": "<account_id>", "name": "Current account", "created_at": "..."}

# Upload a statement: the raw file as the body, Content-Type text/csv (not multipart)
curl -s -X POST localhost:8000/accounts/<account_id>/imports -H "$AUTH" \
  -H 'Content-Type: text/csv' --data-binary @tests/fixtures/statement_mixed_errors.csv
# -> 201 {"id": "<import_id>", "status": "completed", "total_rows": 8, "valid_rows": 2,
#         "invalid_rows": 6, "duplicate_rows": 0, "imported_rows": 2, "error": null, ...}

curl -s localhost:8000/imports/<import_id> -H "$AUTH"
curl -s 'localhost:8000/imports/<import_id>/errors?limit=100' -H "$AUTH"   # page with after_row=
```

Interactive docs: http://127.0.0.1:8000/docs.

| Endpoint | Success | Errors |
|---|---|---|
| `GET /me` | 200 | 401 |
| `POST /accounts` | 201 | 401, 413 (body over 64 KiB), 422 |
| `GET /accounts`, `GET /accounts/{id}` | 200 | 401, 404 |
| `POST /accounts/{id}/imports` | 201 (also when the file is rejected: `status: "failed"`); 200 when the same file was already rejected with the same error | 401, 404, 409 duplicate, 413, 415, 500 |
| `GET /imports/{id}` | 200 | 401, 404 |
| `GET /imports/{id}/errors?after_row=&limit=` | 200 | 401, 404, 422 |
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
               format, import outcome
  data/        SQLAlchemy models, engine/session factories, owner-scoped queries, DB error helpers
  services/    use cases and transaction boundaries (accounts, imports, users and tokens)
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

## Decisions

- [ADR 0001 — Money: signed minor units, no rounding](docs/adr/0001-money-signed-minor-units.md)
- [ADR 0002 — CSV parsing: strict format, row-level errors](docs/adr/0002-csv-parsing-policy.md)
- [ADR 0003 — Persistence: ownership in the schema, CHECK over ENUM, isolated test databases](docs/adr/0003-persistence-schema.md)
- [ADR 0004 — Import pipeline: synchronous, one transaction, partial import](docs/adr/0004-import-pipeline.md)
- [ADR 0005 — API conventions: error envelope, no echoed values, placeholder owner](docs/adr/0005-api-conventions.md)
- [ADR 0006 — Deduplication: exact keys, multiset union, enforced by a unique constraint](docs/adr/0006-deduplication.md)
- [ADR 0007 — Authentication: opaque API tokens, stored hashed, issued by a CLI](docs/adr/0007-authentication.md)
