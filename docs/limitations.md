# Known limitations

What the project does not do. Back to the [README](../README.md).

- One CSV dialect. Real bank exports (UK `DD/MM/YYYY` dates, separate debit/credit columns,
  `1,234.56` amounts) need explicit format adapters; none exist yet.
- Deduplication is exact (ADR 0006). If a correction changes a row that was already valid (a typo
  in the amount or description), the old row stays and the corrected one is added. If one day is
  split across two exports and each part has a different but identical-looking transaction, the
  second is skipped as a duplicate.
- Duplicates stored before Phase 4 (migration 0003) stay.
- The parser holds the whole file in memory (bounded by the 5 MiB limit).
- Authentication is API tokens only, managed by an operator with shell and database access: no
  sign-up, no password login, no token endpoints. Failed attempts count against the general rate
  limit only; there is no stricter limit or lockout for them, and no "last used" time (ADR 0007,
  ADR 0010).
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
- The rate limit lives in one process's memory (ADR 0010): it resets on restart, does not hold
  across several workers or hosts, and a client with more than 10 000 addresses (or IPv6 /64s) is
  not limited. A refused request still costs the one event loop its parsing and a 429: a flood is
  kept away from the database, not from the CPU. Shedding floods needs limits in front of the
  server (proxy or firewall).
- One app process: every deploy has a few seconds of downtime, and there are no metrics.
