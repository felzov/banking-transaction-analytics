# Ledger accounts

Accounts fed by banking-ledger-api (ADR 0009). Back to the [README](../README.md).

An account takes its transactions either from CSV uploads or from one
[banking-ledger-api](https://github.com/felzov/banking-ledger-api) account, chosen when it is created.
The server needs `LEDGER_API_URL` (e.g. `http://127.0.0.1:8001`; unset = syncs answer 503).

```bash
# Link: the ledger's own user id and account id
curl -s -X POST localhost:8000/accounts -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Ledger GBP", "ledger": {"user_id": "<ledger_user_id>", "account_id": "<ledger_account_id>"}}'
# -> {"id": "<account_id>", "source": "ledger", "ledger": {"user_id": "...", "account_id": "..."}, ...}

# Sync: log in to the ledger (POST /sessions there) and pass its 30-minute token
curl -si -X POST localhost:8000/accounts/<account_id>/ledger-syncs -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"ledger_token": "lt_..."}'
# -> 201, Location: http://localhost:8000/ledger-syncs/<sync_id>
#    {"id": "<sync_id>", "account_id": "...", "fetched_entries": 5, "duplicate_entries": 0,
#     "imported_entries": 5, "created_at": "..."}
# The same again with nothing new in the ledger -> 204, no body, nothing recorded
```

- The ledger token is used for that sync only: never stored, logged or returned.
- Each ledger entry becomes one transaction: booked on the **UTC date** of its posting time,
  described as `Ledger deposit`, `Ledger withdrawal`, `Ledger transfer in` or `out`, in the ledger
  account's currency, with its `ledger_entry_id`. Totals of a ledger account equal its ledger
  balance.
- Re-syncing is safe: entries are matched by the ledger's entry id. A sync reads newest first and
  stops at the first page with an entry the account already has, so a sync without news reads one
  page and answers 204 without recording a sync. When there is news, `duplicate_entries` counts
  the stored entries it read again.
- Only the ledger's currencies (GBP, EUR) are accepted, because ledger amounts are taken over in
  their minor units as they are; a posting time whose UTC date falls outside years 1–9999 is an
  invalid response.
- Nothing is stored unless the whole statement checks out: shapes, signs, and the running balance
  from entry to entry (a history read to its start must begin at zero).
- A ledger account takes no CSV uploads and a CSV account no syncs (409 `account_source_mismatch`).
  One owner can link a ledger account once (409 `ledger_account_already_linked`).

| Sync error | Status | When |
|---|---|---|
| `invalid_ledger_token` | 422 | Not a ledger token (`lt_` + 43 characters); checked before anything else |
| `ledger_token_rejected` | 422 | The ledger answered 401: log in to the ledger again |
| `ledger_account_unavailable` | 422 | The ledger answered 403/404: the link names another ledger user, or a missing account |
| `ledger_too_many_entries` | 422 | The sync would read more than `LEDGER_SYNC_MAX_ENTRIES` entries (the new ones, plus the stored ones on its last page) |
| `ledger_unavailable` | 502 | Unreachable, timed out, 429 or 5xx |
| `ledger_invalid_response` | 502 | Malformed or inconsistent statement; nothing stored |
| `ledger_not_configured` | 503 | `LEDGER_API_URL` is not set |

| Setting | Default | |
|---|---|---|
| `LEDGER_API_URL` | unset | Base URL of banking-ledger-api, http(s), no `user:password@`. The only place a ledger call's destination comes from |
| `LEDGER_TIMEOUT_SECONDS` | 10 | Per ledger call |
| `LEDGER_SYNC_TIMEOUT_SECONDS` | 120 | One deadline for all ledger calls of a sync |
| `LEDGER_SYNC_MAX_ENTRIES` | 50000 | Entries one sync may read |
