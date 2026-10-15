# ADR 0002 — CSV parsing: strict format, row-level errors

- Status: Accepted
- Date: 2026-10-15

## Context

Bank CSV exports are inconsistent: encodings, date formats, separators, column names and
quoting all vary. A user importing a statement needs to know exactly which rows were rejected
and why, without the whole import failing because of one bad line. Statements contain personal
financial data.

## Decision

1. Define one canonical format (v1, documented in the README). Bank-specific formats will be
   handled later by adapters that convert into it, not by making the core parser lenient.
2. Two error levels:
   - **File errors** reject the whole file: empty, too large, not UTF-8, malformed quoting,
     missing or duplicate required columns, too many rows, no data rows.
   - **Row errors** reject a single row and are collected; parsing continues.
3. The parser is a pure function `bytes -> ParseResult(transactions, errors)`. It does no I/O
   and makes no policy decision about whether "too many" row errors should fail the import;
   that belongs to the import job (Phase 3).
4. Error messages are fixed text plus stable codes. They never include values from the file.
5. Limits are explicit: 5 MiB and 50 000 data rows by default. Invalid rows count toward the row
   limit so a file of garbage cannot bypass it.
6. The parser does not deduplicate. Identical-looking rows can be different real transactions
   (two coffees on the same day); deduplication is a separate, explicitly tested step.

## Consequences

- Behaviour is deterministic and easy to test without a database.
- Real bank exports will initially be rejected until an adapter exists. This is deliberate:
  a wrong guess about a date format (`01/02` = 1 Feb or 2 Jan?) corrupts data silently.
- Whole-file in-memory parsing is fine within the 5 MiB limit; streaming can come later if
  measured file sizes require it.
