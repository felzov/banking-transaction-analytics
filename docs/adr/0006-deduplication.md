# ADR 0006 — Deduplication: exact keys, multiset union, enforced by a unique constraint

- Status: Accepted; extended by ADR 0009 (ledger rows: fingerprint of the entry id, occurrence 1)
- Date: 2026-10-18

## Context

Until Phase 4 the only duplicate check was file-level (same bytes → 409, ADR 0004). Three common
uploads still stored rows twice: a corrected file (a file with row errors is partly imported; the
fixed file has a different hash), overlapping statements (1–30 September, then 15 September –
15 October), and the same file with and without a UTF-8 BOM. At the same time, identical-looking
rows inside one file are different real transactions (two coffees on one day, ADR 0002 item 6),
so "drop identical rows" is wrong.

## Decision

1. **Key**: `(owner_id, account_id, booked_on, amount_minor, currency, normalized_description)`.
   Exact match only. `normalized_description` already absorbs whitespace, case and Unicode
   compatibility forms (NFKC); the display description and the row number are not part of the key.
2. **Multiset union by maximum**: if a file contains a key `n` times and the account already holds
   it `m` times, the import adds `max(0, n − m)` rows. Overlap → nothing added; corrected file →
   only the fixed rows; a partial export with one coffee followed by the full one with two → one
   added.
3. **Enforced by the database**: every transaction stores
   - `fingerprint`: SHA-256 (BYTEA, 32 bytes) of
     `iso_date \x1f amount_minor \x1f currency \x1f normalized_description` in UTF-8
     (`domain/dedup.py`). The unit separator cannot appear in a description: `clean_description`
     collapses it as whitespace (ADR 0003 item 13 rejects every other control character).
   - `occurrence`: the k-th row with that fingerprint, numbered in file order.

   `uq_transactions_dedup UNIQUE (owner_id, account_id, fingerprint, occurrence)`, and the import
   inserts every valid row with `ON CONFLICT ON CONSTRAINT uq_transactions_dedup DO NOTHING
   RETURNING row_number`. Occurrences `1..m` already exist, so exactly `m+1..n` are inserted.
   Like the file check (ADR 0004 item 6) there is no SELECT before the INSERT and no explicit
   lock, but the insert does wait: a concurrent import that meets an uncommitted conflicting row
   blocks until the other import ends, then skips the row if it committed or inserts it if it
   rolled back. Only this constraint is skipped; every other violation still raises.
4. **Rows are inserted in key order `(fingerprint, occurrence)`, never file order.** Because the
   insert waits on the other import's uncommitted rows, two imports sharing keys K1 and K2 in
   opposite file order would each hold one and wait for the other: PostgreSQL aborts one with a
   deadlock, and a valid upload ends as `internal_error`. One global order makes a cycle
   impossible. Which copy of a key is new does not change, because occurrences are numbered in
   file order before sorting. `tests/integration/test_committed_imports.py` forces that
   interleaving. The sort runs in the parse worker thread; `insert_transactions` sorts again to
   guarantee the order (one linear pass on sorted rows). In key order the rows of one batch are
   scattered in memory, which raised the longest event-loop stall from ~0.03 s to ~0.08 s with
   1 000-row batches; batches are now 250 rows (ADR 0004 item 8), which restores ~0.03 s at the same
   total time.
5. **Hash, not the raw columns**: `normalized_description` can reach 1 500 characters, about 3 KB
   in UTF-8 after casefolding, over PostgreSQL's ~2.7 KB limit for a btree entry.
6. **Counted, not listed**: `import_jobs.duplicate_rows` (CHECK `0 <= duplicate_rows <= valid_rows`);
   the API returns it with `imported_rows = valid_rows − duplicate_rows`. It is known only after the
   INSERT, so it is set by an UPDATE in the import transaction (amends ADR 0004 item 3: the job is
   inserted in its final state except this counter). Invariant: stored transactions of a job =
   `valid_rows − duplicate_rows`.
7. **File-level 409 stays** for identical bytes: a cheap, clear signal that adds no job.
8. **Fingerprints and occurrences are computed in the parse worker thread** (0.14 s per 50 000 rows,
   next to 0.37 s of parsing).
9. **Migration 0003 backfills existing rows in SQL** (`sha256(convert_to(...))`, `to_char` for the
   date, `row_number()` for occurrences). Rows that earlier re-imports duplicated stay, as
   occurrences 2, 3, … of their key. A test compares the SQL result with `domain/dedup.py`.

## Measurements (local, 50 000 rows, all distinct)

| | Time | Longest event-loop stall |
|---|---|---|
| First import | 3.3–3.5 s | 0.03 s |
| Same rows again (all duplicates) | 1.8–2.0 s | 0.02–0.03 s |

Key-order inserts with 1 000-row batches measured 0.06–0.12 s stalls; see item 4.

## Consequences

- Re-uploading corrected, overlapping or BOM-variant files is safe; the response says how many rows
  were already there.
- **Changing the fingerprint format or `normalize_description` breaks matching against stored rows**
  unless a migration recomputes every fingerprint. `tests/unit/test_dedup.py` pins one fingerprint
  so such a change fails loudly.
- **False negative (row dropped)**: if one day is split across two exports and each part has a
  different but identical-looking transaction, the second is treated as the first. Bank exports are
  cut by date, so a day normally appears whole in both.
- **False positive (row kept twice)**: if a correction changes a *valid* row (a typo in the amount or
  description), the old row stays and the new one is added. No fuzzy matching.
- Duplicates stored before Phase 4 stay.
- An import that shares keys with a slow concurrent import waits for it (up to the length of that
  import's transaction, ~3 s for 50 000 rows). Any future writer of transactions must insert in the
  same key order, or the deadlock comes back.
- Which skipped row matched which stored transaction is not recorded.
- Transactions from another source (Phase 7) need a fingerprint and occurrence too, or a decision to
  exclude them from dedup. → ADR 0009: a ledger row's fingerprint is the hash of its ledger entry
  id, its occurrence 1; it uses this constraint and the same key-order inserts.

## Alternatives considered

- **Lock the account row (`SELECT … FOR NO KEY UPDATE`), count existing rows in the file's date
  range, insert the difference.** No new columns and the job stays written once, but correctness
  depends on every writer taking the lock, and existing rows are read back to the application.
- **Dedup by the bank's transaction reference.** The most precise key, but CSV v1 has no such column;
  it belongs to future format adapters.
- **Fuzzy matching** (±1 day, similar descriptions): silent data loss in financial records.
- **Drop the file-level 409** and rely on row dedup alone: simpler, but every accidental re-upload
  would add a job of pure duplicates.
- **Store skipped rows per import** (row number → matched transaction): more transparent, more
  schema; deferred until someone needs it.
