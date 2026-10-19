# ADR 0007 — Authentication: opaque API tokens, stored hashed, issued by a CLI

- Status: Accepted
- Date: 2026-10-19

## Context

Until Phase 5 every request acted as one configured owner (`DEV_OWNER_ID`, ADR 0005 item 5), and
`owner_id` referred to nothing (ADR 0003 item 1). The API has no UI: its clients are scripts and
curl. The data is personal and financial, so a caller must not learn whether a token or another
owner's resource exists.

## Decision

1. **Personal API tokens, sent as `Authorization: Bearer <token>`.** No passwords: nothing to hash
   slowly, no login endpoint to brute-force, no password policy. A token is `bta_` + 43 base64url
   characters (32 bytes from `secrets`). The prefix lets secret scanners recognise a leaked token,
   and a header that does not have this shape is refused before any query.
2. **Issued and managed by an operator CLI** (`python -m transaction_analytics.cli`): `create-user`,
   `issue-token`, `list-tokens`, `revoke-token`, `disable-user`. The token is printed once, to
   stdout. There are no token endpoints in the API yet; `GET /me` shows whose token it is.
3. **Opaque, not JWT.** One indexed lookup per request (every request already queries the database)
   buys immediate revocation and expiry with no signing keys to manage.
4. **Stored as SHA-256 only** (`api_tokens.token_sha256`, 32 bytes, UNIQUE). A 256-bit random token
   cannot be recovered from its hash by guessing, so a slow hash (argon2, scrypt) adds nothing and
   would cost CPU on every request. No pepper: it would protect only low-entropy secrets.
5. **A token is live** when it is not revoked, `expires_at > now()` (database clock, ADR 0003) and
   its user is not disabled. Tokens always expire: 1–365 days, 90 by default. Revoking and disabling
   keep the first timestamp when repeated.
6. **One 401 for every failure** (missing header, other scheme, malformed, unknown, expired,
   revoked, disabled user): `{"error": {"code": "not_authenticated", ...}}` with
   `WWW-Authenticate: Bearer`. Nothing tells a caller which tokens exist; another owner's resources
   stay 404 (ADR 0005 item 4).
7. **The owner is a user.** `users` is the only table without `owner_id`; `api_tokens.owner_id` and
   `accounts.owner_id` reference `users(id)` (`ON DELETE RESTRICT`). The other tables already reach
   `accounts (owner_id, id)` through their composite foreign keys, so every `owner_id` now names a
   user without further keys. Migration 0004 creates a user ("Migrated owner") for every owner that
   already had accounts, so Phase 3–4 data stays reachable: issue a token for that id.
8. **`get_owner_id` keeps its name and type** (ADR 0005 item 5): routes and services did not change.
   It parses the header with FastAPI's `HTTPBearer(auto_error=False)`, which also documents the scheme
   in OpenAPI, and calls `services.auth.authenticate`.
9. **The token lookup is the one query not scoped by an owner** (`data.auth.find_token_owner`): it is
   how the owner is found. Every other user and token query takes `owner_id`; `revoke-token` needs
   the user id as well as the token id.
10. **Authentication ends its read transaction.** Dependencies are solved before the upload route
    streams its body (up to 5 MiB), so the lookup's transaction would otherwise stay open, holding a
    pooled connection "idle in transaction", for as long as the client takes to send it (ADR 0004
    item 10). `authenticate` rolls back after the lookup; this costs one more round trip per request.
    It also means unauthenticated uploads get 401 before their body is read (no 413/415 for them).
11. **Every route except `/health` requires a token.** `tests/api/test_auth_contract.py` checks each
    OpenAPI operation for the bearer scheme and pins the route list, so a new route cannot ship
    without authentication by accident. `/docs` and `/openapi.json` stay public: they describe the
    API, not anyone's data.
12. **`DEV_OWNER_ID` is gone.** A value left in an old `.env` is ignored, never a back door.

## Consequences

- Clients must send a token; the README shows how to get one.
- Leaked token: revoke it with the CLI; it stops working on the next request.
- Operators need shell and database access to manage users and tokens.
- Tests that are not about authentication still override `get_owner_id`; the integration fixtures
  commit users rows for their fixed owners.

## Known limitations

- No rate limiting of failed attempts. Guessing is hopeless (256 bits), but each well-formed fake
  token costs one indexed query; a proxy or Phase 8 should limit request rates.
- No `last_used_at`: writing it on every request would turn reads into writes. Unused tokens are
  found by their expiry instead.
- No self-service: no sign-up, no token endpoints, no password login. Add them when a UI needs them.
- No re-enabling of a disabled user and no user deletion (data is `RESTRICT`ed).
- A token is checked once per request; a request that is already running when its token is revoked
  finishes.

## Alternatives considered

- **Email and password with a login endpoint** (argon2-cffi, or stdlib `hashlib.scrypt`): needed for
  people using a UI, but adds password storage, login rate limiting, timing-safe user lookup and a
  password policy — about twice the scope, for clients that are scripts today.
- **JWT / OIDC** (`pyjwt[crypto]`): stateless, but a token cannot be revoked before it expires, and
  keys or an identity provider must be configured.
- **`X-Owner-Id` header**: rejected in ADR 0005; any caller could claim any owner.
- **A separate session for the lookup** instead of rolling back: needs its own sessionmaker in the
  dependency, which the tests' session override would not reach.
