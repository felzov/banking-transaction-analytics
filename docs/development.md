# Development

Database, migrations, tests and the project layout. Back to the [README](../README.md).

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
  ([ADR 0003](adr/0003-persistence-schema.md)).
- Print the SQL without running it: `uv run alembic upgrade head --sql`.

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

## Project layout

```text
src/transaction_analytics/
  domain/      pure logic (no I/O): money, field rules, CSV parser, dedup keys, names, API token
               format, import outcome, analytics values (date range, cursor, totals), ledger
               entries and statement checks
  data/        SQLAlchemy models, engine/session factories, owner-scoped queries, DB error helpers
  integrations/ HTTP client for banking-ledger-api (outbound I/O, no business rules)
  services/    use cases and transaction boundaries (accounts, imports, ledger syncs, users, tokens)
  api/         FastAPI routers (thin), dependencies (incl. authentication), schemas, error handling,
               body size and rate limits
  cli.py       operator commands for users and API tokens (thin, like api/)
  settings.py  configuration from environment / .env
  log_config.py handler and format for the app's own loggers
  main.py      app factory
migrations/    Alembic environment and versions
tests/
  unit/        domain and settings tests, schema guards (no DB)
  api/         HTTP tests that need no database (error contract, upload checks)
  integration/ tests against a real PostgreSQL, including the API end to end (marker: integration)
  fixtures/    anonymized sample statements only
docs/adr/      architecture decision records
```
