# ADR 0008 — Analytics: per-account, per-currency SQL totals; money in API responses

- Status: Accepted
- Date: 2026-10-20

## Context

Phases 3–5 store an account's transactions but give no way to read them back. Phase 6 adds the
first read endpoints for transactions and the first API responses that contain money. Constraints
from earlier decisions: amounts are signed BIGINT minor units, negative = money out (ADR 0001);
amounts in different currencies are never added together, there is no FX (ADR 0001 item 5); every
query is scoped by owner (ADR 0005 item 4); deduplication is exact and has known false positives
and negatives (ADR 0006). One account can hold hundreds of thousands of rows.

## Decision

1. **Three endpoints, all per account**: `GET /accounts/{id}/transactions` (listing),
   `GET /accounts/{id}/summary` (totals per currency) and `GET /accounts/{id}/summary/monthly`
   (totals per month and currency). Nothing spans accounts: transfers between one owner's own
   accounts would count as both inflow and outflow, and telling them apart needs data CSV v1 does
   not have.
2. **Totals are per currency, always.** Each row has `transactions` (count), `inflow` (sum of
   amounts > 0), `outflow` (sum of amounts < 0, kept negative) and `net = inflow + outflow`. A
   zero amount is counted but is in neither sum. There is no grand total. A range without
   transactions returns no rows rather than zeros, because the API cannot know which currencies to
   report zeros for.
3. **Aggregated in SQL** (`GROUP BY currency`, `sum(...) FILTER (WHERE ...)`), never by loading
   rows into the application. `sum(bigint)` is `numeric` in PostgreSQL, so a sum cannot overflow
   even though a single amount may be ±(2⁶³−1); asyncpg returns an exact `Decimal`, which the data
   layer turns into an `int` (exact for integral values). Two maximal amounts sum to 2⁶⁴−2 exactly
   (test).
4. **Money in JSON**: every amount is two fields made from one int: `<name>_minor`, a JSON integer
   in minor units (what clients compute with), and `<name>`, the same value as an exact decimal
   string (`"-12.30"`, JPY `"1500"`). `domain.money.format_minor` is the exact inverse of
   `parse_amount_minor` (tested on random values across the BIGINT range) and never rounds. The
   string is there because many JSON parsers (JavaScript's among them) read integers above 2⁵³
   as floats, and sums can exceed that; it is also what a curl user can read.
5. **Date ranges**: optional `from` and `to`, both inclusive, `YYYY-MM-DD` only (like statement
   dates, ADR 0002; pydantic's own `date` would also accept Unix times and midnight datetimes).
   `from > to` is 422 `invalid_date_range`. Validation happens before any query.
6. **Months** are calendar months of `booked_on`, computed as
   `date_trunc('month', booked_on::timestamp)::date`. The explicit `timestamp` (without time
   zone) matters: on a `date`, `date_trunc` resolves to the `timestamptz` overload and would
   involve the session's TimeZone. Months without transactions are left out; clients fill gaps.
7. **Listing is newest first with an opaque keyset cursor**: `ORDER BY booked_on DESC, id DESC`,
   `limit` 1–500 (default 100), `next_cursor` from a `limit + 1` probe (no COUNT). The cursor is
   base64url of `booked_on/id`; only the exact form the server produces is accepted (422
   `invalid_cursor` otherwise). It is a position, not a credential: the query is still scoped by
   owner and account, so a forged cursor can at most skip rows of the caller's own account. Ties on
   one day are broken by the random id: stable, but not the file order.
8. **One index**, `ix_transactions_owner_id_account_id_booked_on_id`
   `(owner_id, account_id, booked_on, id)` (migration 0005), serving all three queries: the date
   range of the summaries and the cursor of the listing (read backwards). Built without
   `CONCURRENTLY`, so it blocks writes to `transactions` while it builds; fine at this stage.
9. **Reads ignore import jobs.** The queries read `transactions` only, so transactions from another
   source (Phase 7) will be included without a change; whether they should be is Phase 7's call.
10. **Ownership as everywhere**: the service validates the query, checks the account belongs to the
    owner (404 `account_not_found` otherwise, also for an account that does not exist), then runs
    one query that filters by owner *and* account.

## Measurements (local, PostgreSQL 17, 100 000 rows: 50 000 in the measured account)

`EXPLAIN ANALYZE` execution time, median of 5:

| Query | With index | Without |
|---|---|---|
| Summary, whole account | 21.9 ms | 21.9 ms |
| Summary, one month | 0.95 ms | 10.0 ms |
| Monthly, whole account | 28.5 ms | 28.8 ms |
| Listing, first page | 0.06 ms | 12.4 ms |
| Listing, page from a cursor | 0.07 ms | 11.8 ms |

A whole-account summary reads every row of the account either way; the index pays for ranges and
for the listing, which without it sorts the whole account on every page.

## Consequences

- Clients get totals they can trust per currency, and can page through any account.
- Analytics inherit deduplication's limits (ADR 0006): a corrected *valid* row stays twice and
  counts twice; a day split across two exports can lose an identical-looking transaction.
- Two fields per amount: more bytes, but nobody has to know exponents to read a response.
- uvicorn's access log records query strings: dates and cursors (a booking date and a
  transaction id). No amounts or descriptions are ever in a URL.
- A request for a whole large account takes tens of milliseconds and grows linearly; if accounts
  grow far beyond this, a monthly rollup table is the next step.

## Alternatives considered

- **Totals across all of an owner's accounts**: easy to query, wrong in the presence of transfers
  between own accounts (item 1).
- **Positive "spending"**: a second sign convention next to the stored one.
- **Only integers, or only decimal strings**: integers lose precision in many JSON clients; strings
  force every client to parse before computing.
- **Offset pagination**: slower with depth, and rows shift while imports run.
- **Zero rows for empty months** (`generate_series`): more SQL, and the currencies to report are
  unknown.
- **Grouping by description ("top merchants")**: real bank descriptions carry changing references,
  so exact grouping is mostly noise; it needs format adapters and merchant normalization first.
- **`INCLUDE (amount_minor, currency)` for index-only summaries**: not needed at the measured cost.
