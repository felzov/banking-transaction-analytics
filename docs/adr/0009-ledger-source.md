# ADR 0009 — Ledger source: accounts fed by banking-ledger-api, pulled per request

- Status: Accepted
- Date: 2026-10-22

## Context

Until Phase 7 every transaction came from an uploaded CSV file and belonged to an import job
(`transactions.import_job_id NOT NULL`). Phase 7 adds a second source: banking-ledger-api, the
double-entry ledger in the same portfolio. Earlier decisions left three questions for this phase:
how the schema keeps ownership checked once a transaction has no import job (ADR 0003 item 2),
how ledger rows are deduplicated (ADR 0006), and whether analytics should count them (ADR 0008
item 9).

What the ledger offers (its README and ADR 0012):

- `GET /users/{user_id}/accounts/{account_id}/statement?limit=&cursor=`: entries newest first,
  each `{entry_id, transaction_id, kind, amount_minor, balance_after_minor, created_at}`, plus the
  current `balance_minor` and `next_cursor`. `limit` is 1–100. Following the cursor never repeats
  or skips an entry; postings made meanwhile appear only on a fresh first page. Transactions are
  immutable.
- **The statement is in commit order, not `created_at` order** (ledger ADR 0012): entries are
  ordered by a per-entry sequence number drawn while the posting holds the account's row lock, so
  for one account it increases in commit order. Its "prefix property": an entry can never commit
  below one already committed. A database trigger there refuses an entry numbered below the
  account's latest.
- No descriptions, no booking dates (only `created_at`, the posting's start time), and no currency
  on the statement; the account (`GET /users/{u}/accounts/{a}`) has it. GBP and EUR only.
- Login by email and password for a bearer token (`lt_` + 43 characters) that lives 30 minutes,
  with no refresh. Another user's `user_id` is 403, a missing or foreign account 404.

## Decision

1. **Pull over HTTP, on request.** `POST /accounts/{id}/ledger-syncs` reads the linked ledger
   account's statement and stores the entries the account does not hold yet, synchronously, like
   an upload (ADR 0004). No background job, no schedule. 201 with the recorded sync when there
   were new entries; **204 and nothing recorded when there were none** (including when a
   concurrent sync stored them first, which is rolled back): a client polling for news would
   otherwise add a `ledger_syncs` row per poll, the growth decision 9 avoids for failures.
2. **The caller brings a ledger token, which is never stored.** The body is
   `{"ledger_token": "lt_..."}`: the caller logs in to the ledger and passes the 30-minute token.
   It is used for this sync's calls and forgotten: not in the database, not in a log line, not in a
   response (tests check all three). The body, not a header or the query string, so it is not in
   access logs; `SecretStr` masks it in reprs, OpenAPI marks it `writeOnly`. Its shape is checked
   before anything else, so text that cannot be an HTTP header value never reaches the client.
3. **One source per account, fixed at creation.** `POST /accounts` with
   `"ledger": {"user_id", "account_id"}` creates a ledger account; without it, a CSV account. A
   ledger account takes no uploads, a CSV account no syncs (409 `account_source_mismatch`). So no
   rows of the two sources ever need to be matched against each other (ledger entries have no
   descriptions to match on), and per-account analytics never mix them. One owner links a given
   ledger account once (409 `ledger_account_already_linked`); two owners may each link it, because
   the ledger checks on every sync that the token's user may read it. The link is not checked at
   creation: that would need a token there too.
4. **Schema (migration 0006).**
   - `accounts.source` (`csv` | `ledger`, CHECK; existing accounts are `csv`) and
     `ledger_user_id`, `ledger_account_id`, set exactly for ledger accounts.
   - `ledger_syncs`: one row per sync, like `import_jobs` for files: `fetched_entries`,
     `duplicate_entries` (`0 <= duplicate <= fetched`).
   - `transactions.source`, and either `import_job_id` + `row_number` (csv) or `ledger_sync_id` +
     `ledger_entry_id` (ledger): `ck_transactions_source_columns`. The two provenance foreign keys
     are composite with `owner_id` and `account_id`, as before; each is skipped for rows of the
     other source, because a key containing a NULL column is not checked.
   - **`fk_transactions_account_source (owner_id, account_id, source) → accounts (owner_id, id,
     source)`** replaces `fk_transactions_account`. It is checked for every row (no nullable
     column), so the database itself stops a CSV row in a ledger account and the reverse, not only
     the service. It needs `UNIQUE (owner_id, id, source)` on accounts. It also makes an account's
     source effectively immutable once it has transactions (the key would block the update).
5. **Deduplication by the ledger's entry id.** A ledger row's fingerprint is
   `sha256("ledger" \x1f entry_id)` and its occurrence is always 1, so the existing
   `uq_transactions_dedup` holds each entry once per account and the import's machinery applies
   unchanged: `ON CONFLICT DO NOTHING`, inserts in key order (ADR 0006 item 4, so concurrent syncs
   cannot deadlock), skipped rows counted as `duplicate_entries`. `ck_transactions_ledger_fingerprint`
   recomputes the hash in SQL from `ledger_entry_id`, so a ledger row cannot carry another key.
   A CSV key starts with an ISO date and a ledger key with `ledger`, so the two never serialize to
   the same string. `tests/unit/test_ledger.py` pins one fingerprint.
6. **Mapping an entry.** `booked_on` is the **UTC date** of `created_at`: an instant has no
   booking day of its own, and UTC is the one choice that needs no per-account setting.
   `description` is fixed text from kind and sign (`Ledger deposit`, `Ledger withdrawal`,
   `Ledger transfer in` / `out`). The currency is the ledger account's, read on every sync, and
   must be in `LEDGER_MINOR_UNIT_EXPONENTS` (GBP and EUR, both 2, copied from the ledger's
   `domain/currency.py`): `amount_minor` is stored as it is, which is exact only where the
   ledger's minor unit is ours, so a currency without a known ledger exponent is refused rather
   than guessed (USD exists here but not at the ledger). A unit test compares the two tables.
   Amounts keep the ledger's sign, which is ADR 0001's (negative = money leaving the account).
7. **Incremental: stop at the first page holding a stored entry.** A sync stores all its entries
   or none, and the ledger's prefix property (Context) means a new entry can only ever appear
   above every committed one, so the entries an account holds are always the oldest part of its
   history: once a page contains a stored entry, everything older is stored too. This would be
   wrong if the statement were ordered by `created_at` (a posting that starts early and commits
   late would land below entries already synced); the ledger orders by commit order precisely to
   rule that out. A sync without new postings reads one page. Each page's "already stored?" query is its own
   short transaction; no connection is held while the ledger is called (ADR 0007 item 10).
   Checking the statement, mapping and sorting the entries run in a worker thread, like the CSV
   parse (ADR 0004 item 2): on the event loop they stalled every other request for 0.38 s at
   50 000 entries; in a thread the longest stall is 0.03 s (measured locally).
8. **The ledger is untrusted input.** Nothing it returns is stored unchecked:
   - Responses: status 200 only (redirects are not followed), uncompressed only (`Accept-Encoding:
     identity`; any other `Content-Encoding` is refused, so the limit counts bytes on the wire and
     nothing is ever inflated), at most 1 MiB, strict JSON shapes
     (amounts must be JSON integers, times must have a time zone and a UTC date within years
     1–9999, unknown kinds and currencies are refused), the account id must be the one asked for, at most 100 entries per page. Unknown
     fields are ignored so the ledger can grow.
   - The statement (pure checks in `domain/ledger.py`): no zero amounts and none outside BIGINT,
     deposits positive and withdrawals negative, no repeated entry, the newest entry ends at the
     statement balance, each entry's balance follows from the next older one, and a history read
     to its start begins at zero. A page that does not advance (empty with a cursor, or repeating
     entries) stops the sync.
   - Anything else: 502 `ledger_invalid_response`, nothing stored.
9. **Errors.** Ledger 401 → 422 `ledger_token_rejected`; 403/404 → 422
   `ledger_account_unavailable` (the caller's token or link is wrong: not our own 401, which is
   about our token and carries `WWW-Authenticate`); unreachable, timeout, 429 or 5xx → 502
   `ledger_unavailable`; reading more than `LEDGER_SYNC_MAX_ENTRIES` entries (the new ones plus
   the stored ones on the last page read) → 422 `ledger_too_many_entries`; no `LEDGER_API_URL` → 503 `ledger_not_configured`. **Failed syncs
   are not recorded**: an outage or an expired token in a retry loop would otherwise grow the table
   without bound (the problem ADR 0004 item 6 had to work around). Logs get the failure class only.
10. **Where calls go comes from settings only.** `LEDGER_API_URL` (http or https, without a user
    name or password: httpx2 would send those as Basic auth in place of the ledger token), never a
    request value; paths are built from stored UUIDs and the cursor is passed as a UUID query
    parameter. The HTTP client ignores the environment (`trust_env=False`: no proxy variables, no
    `.netrc`). Timeouts: `LEDGER_TIMEOUT_SECONDS` per call (10 s), and `LEDGER_SYNC_TIMEOUT_SECONDS`
    (120 s) as one deadline for all ledger calls of a sync. The deadline does not cover the
    database queries between calls: cancelling a query halfway leaves its connection unusable, and
    a slow database is not a slow ledger (it fails as our own 500, not 502 `ledger_unavailable`).
11. **`httpx2` becomes a runtime dependency**: the async HTTP client for these calls, and already
    the tests' client (no new package in the lock). The client lives in a new layer,
    `integrations/`: outbound I/O like `data/`, but over HTTP, with no business rules.
12. **Analytics count ledger rows** (settles ADR 0008 item 9): reads stay per account, and an
    account has one source, so nothing is counted twice. `GET /accounts/{id}/transactions` gains
    `source` and `ledger_entry_id`; `import_id` is `null` for ledger rows.

## Consequences

- A ledger account can be analysed like a CSV one; its totals equal the ledger balance (tested).
- Syncing needs a fresh ledger login at most every 30 minutes; nothing secret about the ledger is
  kept here.
- The transaction listing's `import_id` can now be `null`: a breaking change for a client that
  assumed otherwise (there are none yet).
- A ledger account's history longer than `LEDGER_SYNC_MAX_ENTRIES` (default 50 000) cannot be
  synced the first time: the ledger reads newest first, and a sync stores all or nothing. Raise the
  setting, or add resumable syncs, if such accounts appear.
- A sync is synchronous: ~500 ledger calls for 50 000 entries on the first sync. Not measured
  against a real ledger.
- A sync that adds nothing leaves no trace: there is no record of when an account was last
  checked against its ledger, only of when it last changed.
- Changing the ledger fingerprint needs a migration that recomputes it, like the CSV one.
- Downgrading migration 0006 refuses while ledger accounts exist (their rows have no import job).

## Known limitations

- The schema allows an import job on a ledger account (only the service refuses the upload); its
  rows could still not be stored there (`fk_transactions_account_source`). Likewise a sync on a
  CSV account could not store rows.
- The stop rule (decision 7) relies on the ledger's prefix property. If the ledger ever wrote an
  entry below committed ones (its ADR 0012 forbids it, enforced by a trigger), a sync would not
  notice it: entries are matched by id, and the balance chain is only checked across the pages a
  sync reads. Totals would then drift from the ledger balance unnoticed.
- The ledger's token shape (`lt_` + 43 characters) is checked here; if the ledger changes it, this
  check must follow.

## Alternatives considered

- **Upload a ledger statement file** (push): no network calls or tokens, but the ledger has no
  export; users would have to stitch statement pages together.
- **Store ledger credentials** (email and password, encrypted) for background syncs: a stored
  secret for a third party, encryption keys to manage, and the ledger has no long-lived tokens.
- **Mixed sources in one account**: needs matching ledger entries against CSV rows without
  descriptions; every heuristic would silently drop or double money.
- **Generalise `import_jobs`** for syncs: `file_sha256`, `row_number` and its counters are all
  CSV-shaped; a separate table keeps both CHECK sets simple.
- **A separate unique key on `ledger_entry_id`**: a second conflict target next to
  `uq_transactions_dedup`, and the key-order rule would have to cover both.
- **Record failed syncs** like failed imports: more history, but unbounded growth under retries
  (decision 9).
- **A booking time zone per account**: more correct for users far from UTC, more scope; the UTC
  date is documented and can be refined later.
