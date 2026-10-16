# Banking Transaction Analytics

[![CI](https://github.com/felzov/banking-transaction-analytics/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/felzov/banking-transaction-analytics/actions/workflows/ci.yml)

Import, validate, normalize, deduplicate and analyze bank-statement transactions.

**Status: early development (Phase 2 of 8).** What exists today:

- `/health` liveness endpoint (FastAPI).
- A pure, fully unit-tested domain layer: a CSV statement parser that returns valid transactions
  plus row-level errors, with explicit money, date and description rules.
- PostgreSQL schema (SQLAlchemy models + Alembic migrations) for accounts, import jobs,
  transactions and row errors, with ownership and data rules enforced by constraints
  ([ADR 0003](docs/adr/0003-persistence-schema.md)). Nothing writes to it yet.

Not built yet: the import API, deduplication, authentication, analytics.
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

## Tests

| Command | Runs | Needs |
|---|---|---|
| `make test` | unit + API tests (`-m "not integration"`) | nothing |
| `make test-integration` | constraints, migrations, round-trips (`-m integration`) | `make db-up`, `TEST_DATABASE_URL` |
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
| Limits | 5 MiB per file, 50 000 data rows (defaults; configurable per call). |

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
  domain/      pure logic: money, field rules, CSV parser, import statuses (no I/O, no DB)
  data/        SQLAlchemy models, engine/session factories
  api/         FastAPI routers (thin; no business logic)
  settings.py  configuration from environment / .env
  main.py      app factory
migrations/    Alembic environment and versions
tests/
  unit/        domain and settings tests, schema guards (no DB)
  api/         HTTP tests
  integration/ tests against a real PostgreSQL (marker: integration)
  fixtures/    anonymized sample statements only
docs/adr/      architecture decision records
```

## Known limitations

- One CSV dialect. Real bank exports (UK `DD/MM/YYYY` dates, separate debit/credit columns,
  `1,234.56` amounts) need explicit format adapters; none exist yet.
- Identical-looking rows are all kept. Deduplication across imports is planned for Phase 4.
- The parser holds the whole file in memory (bounded by the 5 MiB limit).
- No authentication. `owner_id` exists in every table but is not linked to users until Phase 5;
  nothing writes to the database yet.
- Re-uploading the same file to the same account is rejected by the schema even if the earlier
  import failed; how to retry is decided with the import API / re-import work.
- The schema cannot prevent one CSV row being stored both as a transaction and as a row error,
  or check that job counters match the stored rows. The import service will own that.
- PostgreSQL's server log records CHECK violations with `DETAIL: Failing row contains (...)`,
  i.e. raw row values. Treat database log access as access to personal data. The app itself
  never logs or returns raw database exceptions (ADR 0003).
- Exporting descriptions back to CSV/Excel would need protection against formula injection
  (`=`, `+`, `-`, `@` prefixes). Not handled because there is no export yet.

## Decisions

- [ADR 0001 — Money: signed minor units, no rounding](docs/adr/0001-money-signed-minor-units.md)
- [ADR 0002 — CSV parsing: strict format, row-level errors](docs/adr/0002-csv-parsing-policy.md)
- [ADR 0003 — Persistence: ownership in the schema, CHECK over ENUM, isolated test databases](docs/adr/0003-persistence-schema.md)
