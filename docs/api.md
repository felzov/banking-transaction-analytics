# API reference

Authentication, every endpoint, amounts, totals and paging. Back to the [README](../README.md).

## Authentication

Every endpoint except `/health` needs an API token ([ADR 0007](adr/0007-authentication.md)).
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
# -> {"id": "<account_id>", "name": "Current account", "source": "csv", "ledger": null,
#     "created_at": "..."}

# Upload a statement: the raw file as the body, Content-Type text/csv (not multipart)
curl -s -X POST localhost:8000/accounts/<account_id>/imports -H "$AUTH" \
  -H 'Content-Type: text/csv' --data-binary @tests/fixtures/statement_mixed_errors.csv
# -> 201 {"id": "<import_id>", "status": "completed", "total_rows": 8, "valid_rows": 2,
#         "invalid_rows": 6, "duplicate_rows": 0, "imported_rows": 2, "error": null, ...}

curl -s localhost:8000/imports/<import_id> -H "$AUTH"
curl -s 'localhost:8000/imports/<import_id>/errors?limit=100' -H "$AUTH"   # page with after_row=

# Transactions, newest first; pass next_cursor back as cursor= for the next page
curl -s 'localhost:8000/accounts/<account_id>/transactions?from=2026-09-01&to=2026-09-30&limit=100' \
  -H "$AUTH"
# -> {"items": [{"id": "...", "source": "csv", "import_id": "...", "ledger_entry_id": null,
#                "booked_on": "2026-09-07", "description": "Refund",
#                "currency": "GBP", "amount_minor": 1500, "amount": "15.00"}, ...],
#     "next_cursor": null}

# Totals per currency (from/to optional, both inclusive); /summary/monthly adds "month": "2026-09"
curl -s 'localhost:8000/accounts/<account_id>/summary?from=2026-09-01&to=2026-09-30' -H "$AUTH"
# -> {"account_id": "...", "from": "2026-09-01", "to": "2026-09-30",
#     "totals": [{"currency": "GBP", "transactions": 2,
#                 "inflow_minor": 1500, "inflow": "15.00",
#                 "outflow_minor": -95000, "outflow": "-950.00",
#                 "net_minor": -93500, "net": "-935.00"}]}
```

Interactive docs: http://127.0.0.1:8000/docs.

| Endpoint | Success | Errors |
|---|---|---|
| `GET /me` | 200 | 401 |
| `POST /accounts` | 201 | 401, 409 ledger account already linked, 413 (body over 64 KiB), 422 |
| `GET /accounts`, `GET /accounts/{id}` | 200 | 401, 404 |
| `POST /accounts/{id}/imports` | 201 (also when the file is rejected: `status: "failed"`); 200 when the same file was already rejected with the same error | 401, 404, 409 duplicate or ledger account, 413, 415, 500 |
| `POST /accounts/{id}/ledger-syncs` | 201 with new entries; 204 when there were none | 401, 404, 409 CSV account, 422, 502, 503 (see [Ledger accounts](ledger-accounts.md)) |
| `GET /ledger-syncs/{id}` | 200 | 401, 404 |
| `GET /imports/{id}` | 200 | 401, 404 |
| `GET /imports/{id}/errors?after_row=&limit=` | 200 | 401, 404, 422 |
| `GET /accounts/{id}/transactions?from=&to=&limit=&cursor=` | 200 | 401, 404, 422 |
| `GET /accounts/{id}/summary?from=&to=`, `GET /accounts/{id}/summary/monthly?from=&to=` | 200 | 401, 404, 422 |
| `GET /health` | 200 | 429 (no token needed) |
| `GET /ready` | 200 | 503 `not_ready`, 429 (no token needed; see [Running on a server](deployment.md)) |

Every endpoint, the probes included, can answer 429 `rate_limited` with `Retry-After`
(seconds) when one client address sends too many requests (ADR 0010).

An upload without a valid token gets 401 before its body is read. A JSON body over 64 KiB gets 413
before authentication (the size limit sits in front of the app), and a rate-limited request gets
429 before either.

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
- The app speaks plain HTTP on 127.0.0.1; TLS is the reverse proxy's job
  ([Running on a server](deployment.md)).

### Amounts, totals and paging (ADR 0008)

- Every amount comes twice: `<name>_minor`, a JSON integer in the currency's minor unit (pence,
  cents; yen have none), and `<name>`, the same value as an exact decimal string. Use the string
  if your JSON parser turns large integers into floats (JavaScript does above 2^53). Negative =
  money out.
- Totals are per currency and never added across currencies: `inflow` sums the positive amounts,
  `outflow` the negative ones (it stays negative), `net = inflow + outflow`. A zero amount is
  counted in `transactions` only. No transactions in the range means `"totals": []`, not zeros.
- `from` and `to` are `YYYY-MM-DD`, both inclusive, both optional; `from` after `to` is 422
  `invalid_date_range`. `/summary/monthly` leaves out months without transactions.
- Listing pages hold up to `limit` (1–500, default 100) transactions; `next_cursor` is `null` on
  the last page. A cursor is opaque; a changed one is 422 `invalid_cursor`. Transactions of one day
  are in a stable but arbitrary order.
- Everything is per account; there are no totals across an owner's accounts (transfers between
  them would count twice).
