# Banking Transaction Analytics

Import, validate, normalize, deduplicate and analyze bank-statement transactions.

**Status: early development (Phase 1 of 8).** What exists today:

- `/health` liveness endpoint (FastAPI).
- A pure, fully unit-tested domain layer: a CSV statement parser that returns valid transactions
  plus row-level errors, with explicit money, date and description rules.

Not built yet: database models, the import API, deduplication, authentication, analytics.
The target users (accountants, bookkeepers, freelancers, small businesses) are hypotheses that
have **not** been validated.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
make install   # uv sync
make check     # ruff lint + format check + pytest
make run       # http://127.0.0.1:8000/health and /docs
```

PostgreSQL is not used by the code yet. To start it ahead of Phase 2:

```bash
cp .env.example .env   # set a local-only password
make db-up             # Postgres on 127.0.0.1:5433
```

## Supported CSV format (v1)

| Rule | Behaviour |
|---|---|
| Encoding | UTF-8; a leading BOM (Excel "CSV UTF-8") is accepted. Anything else rejects the file. |
| Header | Required. Columns `date`, `description`, `amount`, `currency` in any order, case-insensitive. Extra columns are ignored. Duplicate names reject the file. |
| `date` | `YYYY-MM-DD` only, must be a real calendar date. |
| `amount` | Plain signed decimal: `-12.30`, `2500`. **Negative = money out (debit), positive = money in (credit).** No currency symbols, thousands separators or exponent notation. More decimal places than the currency allows is an error; nothing is rounded. |
| `currency` | ISO 4217 code: GBP, EUR, USD, JPY (case-insensitive). |
| `description` | Required, max 500 characters after whitespace is collapsed. |
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
  domain/      pure logic: money, field rules, CSV parser (no I/O, no DB)
  api/         FastAPI routers (thin; no business logic)
  main.py      app factory
tests/
  unit/        domain tests
  api/         HTTP tests
  fixtures/    anonymized sample statements only
docs/adr/      architecture decision records
```

## Known limitations

- One CSV dialect. Real bank exports (UK `DD/MM/YYYY` dates, separate debit/credit columns,
  `1,234.56` amounts) need explicit format adapters; none exist yet.
- Identical-looking rows are all kept. Deduplication across imports is planned for Phase 4.
- The parser holds the whole file in memory (bounded by the 5 MiB limit).
- No authentication; nothing is stored.
- Exporting descriptions back to CSV/Excel would need protection against formula injection
  (`=`, `+`, `-`, `@` prefixes). Not handled because there is no export yet.

## Decisions

- [ADR 0001 — Money: signed minor units, no rounding](docs/adr/0001-money-signed-minor-units.md)
- [ADR 0002 — CSV parsing: strict format, row-level errors](docs/adr/0002-csv-parsing-policy.md)
