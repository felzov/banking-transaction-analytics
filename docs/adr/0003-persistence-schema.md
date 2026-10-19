# ADR 0003 — Persistence: ownership in the schema, CHECK over ENUM, isolated test databases

- Status: Accepted; the file-hash uniqueness rule is superseded by ADR 0004 (migration 0002);
  item 1 amended by ADR 0007 (owner_id references users, migration 0004)
- Date: 2026-10-16

## Context

Phase 2 adds PostgreSQL tables for accounts, import jobs, transactions and row errors. Data is
personal and financial; every query must be scoped by owner, and a bug in a query should not be
able to attach one owner's rows to another owner's account. Authentication arrives in Phase 5,
deduplication in Phase 4, a second transaction source (ledger) in Phase 7.

## Decision

1. **owner_id everywhere.** Every table has `owner_id UUID NOT NULL`, even before auth exists
   (tests use a fixed UUID). There is no users table and no FK on `owner_id` until Phase 5.
   → Phase 5 (ADR 0007): `users` is the one table without `owner_id`; `accounts.owner_id` and
   `api_tokens.owner_id` reference `users(id)`, and the composite keys carry it to the rest.
2. **Ownership enforced by composite foreign keys**, not only by queries:
   - `import_jobs (owner_id, account_id) → accounts (owner_id, id)`
   - `transactions (owner_id, account_id, import_job_id) → import_jobs (owner_id, account_id, id)`:
     the transaction's owner *and* account must match its job's.
   - `transactions (owner_id, account_id) → accounts (owner_id, id)`: redundant while
     `import_job_id` is NOT NULL; it becomes the only guard once transactions can exist without
     an import job (Phase 7), because a FK containing a NULL column is not checked.
   - `import_row_errors (owner_id, import_job_id) → import_jobs (owner_id, id)`

   A composite FK needs a UNIQUE constraint on exactly the referenced columns, hence the extra
   `UNIQUE (owner_id, id)` / `(owner_id, account_id, id)`. `owner_id` comes first in all of them,
   so the same indexes serve `WHERE owner_id = ...` lookups.
3. **All FKs are `ON DELETE RESTRICT`.** Financial rows never disappear through a cascade.
4. **Enumerations are `TEXT` + `CHECK (col IN (...))`, not native ENUM types.** Changing a CHECK
   is a transactional drop-and-add in one migration; a native ENUM value cannot be removed or
   renamed without recreating the type, `ADD VALUE` cannot be used in the same transaction, and
   downgrades must remember `DROP TYPE`. The cost is that values exist twice (domain `StrEnum`
   and migration); an integration test compares each CHECK definition with its enum.
   Migrations write the values as **literals** and never import the domain: a migration is
   history and must not change when the code changes.
   Covered: currency (`Currency`), job status (`ImportStatus`), job failure code
   (`ImportFailureCode` = every `FileErrorCode` + `internal_error`), row error code
   (`FieldErrorCode`), row error column (`REQUIRED_COLUMNS`).
5. **Other invariants as CHECKs**: lowercase 64-hex `file_sha256` (otherwise upper/lower case of
   the same hash would pass the uniqueness rule twice); non-negative counts with
   `valid + invalid = total`; `error_code` present iff `failed`, `error_message` iff `error_code`;
   `started_at` iff not `pending`; `finished_at` iff `completed`/`failed`, and not before
   `started_at`; `row_number >= 2` (row 1 is the header, ADR 0002); description 1–500 chars;
   `normalized_description` 1–1500 chars because `casefold()` can triple a string's length.
6. **UUID primary keys** from `gen_random_uuid()`. IDs will appear in URLs; random IDs are not
   enumerable and do not reveal volumes. UUIDv7 (better index locality) is deferred: it needs
   Python 3.14 or PostgreSQL 18, and volumes are small.
7. **Indexes: only those implied by primary keys and unique constraints.** The FK
   `transactions → accounts` has no child-side index; it only matters for deleting an account,
   which nothing does. Dedup (Phase 4) and analytics (Phase 6) add indexes for their real queries.
   → Phase 4 added `uq_transactions_dedup (owner_id, account_id, fingerprint, occurrence)`
   (ADR 0006).
8. **Constraint names are deterministic** (naming convention in `data/models.py`, `op.f()` in
   migrations). Tests assert which constraint fired; Phase 3 maps violations to safe messages by
   `constraint_name`.
9. **CHECKs live only in migrations**, not in the ORM models: Alembic autogenerate does not
   compare CHECKs, so a copy in the models could drift silently. Columns, keys and FKs are in
   both and a test asserts `compare_metadata(...) == []`.
10. **No `DBAPIError` is ever logged or returned via `str()` / `repr()`** — not the SQLAlchemy
    wrapper, not `exc.orig`, not its cause. Exception text cannot be trusted to be free of row
    values, and `tests/integration/test_error_leaks.py` documents both ways it leaks:
    - Server-side violations: `hide_parameters=True` keeps bound values out of SQLAlchemy's text,
      but asyncpg's `detail` carries Postgres's `Failing row contains (...)` with the full row.
    - Client-side encoding errors (`DataError`, raised by asyncpg before the query reaches the
      server, e.g. an integer outside BIGINT): the value is inside the exception message itself,
      so `hide_parameters` does not help and even `str(exc)` of the wrapper leaks it.

    What may be logged: the exception class name (`type(exc.orig.__cause__).__name__`) and
    `constraint_name`. Mapping (Phase 3): `IntegrityError` → a fixed message chosen by
    `constraint_name`; every other `DBAPIError` → `internal_error` with fixed text.
    `hide_parameters=True` stays on as defence in depth.
11. **Configuration**: `DATABASE_URL` via pydantic-settings as `SecretStr`, read lazily
    (`get_settings()`), never stored in `alembic.ini`. No engine is created at import time.
    `.env` is resolved from the current working directory, so the app and Alembic are run from
    the repository root (deployments pass real environment variables instead).
12. **The database must use UTF8 encoding.** The length CHECKs use `char_length()`, which counts
    characters only in UTF8 (in `SQL_ASCII` it counts bytes). Test databases are created with
    `ENCODING 'UTF8' TEMPLATE template0`, and a test asserts `server_encoding = UTF8`.
13. **What the domain accepts, the database must store.** PostgreSQL TEXT cannot hold NUL, so the
    domain rejects control characters (Unicode Cc) in descriptions as a row error
    (`invalid_characters`) instead of letting one row fail the whole insert with a non-integrity
    error. Format characters (Cf: emoji joiners, RTL marks) are kept.

## Test isolation

- Integration tests use `TEST_DATABASE_URL` only, and refuse to start unless the database name
  ends in `_test`. That database is dropped and recreated at the start of each session, then
  migrated with `alembic upgrade head` — so every run starts from a clean upgrade.
- Each test runs inside one outer transaction that is rolled back; the ORM session uses
  savepoints (`join_transaction_mode="create_savepoint"`), so tests can commit and catch
  integrity errors. Faster than TRUNCATE, at the price of not exercising a real COMMIT.
- Migration tests (`upgrade → downgrade base → upgrade`, drift check) get their own throwaway
  database, because a downgrade would destroy the shared schema.
- Engines use `NullPool` and live for one test, so no connection outlives its event loop.
- Unit tests (`-m "not integration"`) need no database. Integration tests fail, not skip, when
  `TEST_DATABASE_URL` is missing, so CI cannot pass by silently running none of them.

## Known consequences and limitations

- `UNIQUE (owner_id, account_id, file_sha256)` also blocks re-uploading a file whose import
  *failed*. Phase 3 must reuse the failed job, or Phase 4 replaces the constraint with a partial
  unique index (`WHERE status <> 'failed'`). → Resolved in ADR 0004: partial unique index.
- The schema cannot stop one CSV row from being stored both as a transaction and as a row error,
  nor check that job counters equal the stored rows; both need triggers. The import service owns
  these invariants (Phase 3) and must test them.
- PostgreSQL's server log records `Failing row contains (...)` for CHECK violations, i.e. row
  values. Operational log access must be treated as access to personal data.
- Adding a currency, status or error code requires a migration that replaces the CHECK.
- A transaction may reference an import job in any status, including `pending` or `failed`;
  the schema cannot express "only completed jobs own transactions" without a trigger. Service
  invariant for Phase 3.
- `TextEnum` raises on a value the code's enum does not know (e.g. code rolled back after a
  migration added a value). Intentional: failing loudly beats silently mislabelling rows.

## Notes for later phases

- **Phase 3 (done, ADR 0004):** set `started_at` / `finished_at` with the database clock
  (`now()`), not the app clock: a backwards step of the app clock between start and finish would
  violate `ck_import_jobs_finished_after_started` and leave the job stuck in `processing`.
- **Phase 3 (done, migration 0002):** validate account names in the domain (the CHECK only trims
  spaces, so a name of tabs or a no-break space passes), then tighten the CHECK to match.
- **Phase 8:** `migrations/env.py` calls `fileConfig(alembic.ini)`. If the app runs migrations
  programmatically, that reconfigures the root logger (level, stderr handler); pass a config
  without logging sections or skip `fileConfig` in that path.
