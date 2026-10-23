# ADR 0010 — Operational readiness: probes, in-process rate limit, app logs, database timeouts

- Status: Accepted
- Date: 2026-10-23

## Context

Phases 0–7 built the API for a developer's machine: `make run` with `--reload` on 127.0.0.1. Earlier
decisions left these for Phase 8:

- `/health` is liveness only; readiness, including the database, was deferred (`api/health.py`).
- No TLS and no rate limiting (ADR 0005); failed token attempts each cost one indexed query
  (ADR 0007).
- If the app ran migrations itself, `env.py`'s `fileConfig` would reset the process's logging
  (ADR 0003, notes for later phases).
- A plain `CREATE INDEX` blocks writes while it builds (migration 0005).

Found while planning: uvicorn's default log config covers only its own loggers, so every
`transaction_analytics.*` record fell through to Python's last-resort handler (WARNING and above,
a bare message with no time, level or logger name; the INFO for client disconnects was dropped).
And no database statement or connection attempt had a time limit.

The target is one host (a VPS), one app process, behind a reverse proxy. No container image.

## Decision

1. **`GET /ready`** answers 200 `{"status": "ready"}` when the database answers and its
   `alembic_version` is exactly `EXPECTED_SCHEMA_REVISION`; otherwise 503 `not_ready` with fixed
   text. Both carry `Cache-Control: no-store`. No token: a deploy script or monitor has none. The
   reason (unreachable, timed out, schema at another revision) goes to the log only, by exception
   class, never its text: connection errors name host and port.
   - The whole check, including waiting for a pooled connection, has 2 s.
   - `EXPECTED_SCHEMA_REVISION` is a constant in `data/schema.py`, not read from `migrations/` at
     runtime; a unit test fails when it is not the newest migration (or the migrations branch).
   - **Exact match**, so the app is not ready before `alembic upgrade head` and not after being
     rolled back below a migration: it then refuses traffic rather than meeting a schema it does
     not know (the same choice as `TextEnum` in ADR 0003).
   - **The ledger is not checked**: it is optional, and its outage already answers 502 on ledger
     syncs alone. Counting it would take CSV imports and analytics down with it.
   - `/health` stays liveness without the database, so a database outage never gets the process
     restarted.
2. **Rate limit per client address, in process** (`api/rate_limit.py`): a token bucket per client,
   `RATE_LIMIT_BURST` (30) at once, refilled at `RATE_LIMIT_PER_MINUTE` (120); `0` turns it off.
   Over the limit: 429 `rate_limited` with `Retry-After` in whole seconds.
   - It is ASGI middleware outside the 64 KiB body limit, so a refused request is not read and
     never reaches the token lookup: beyond the limit, a flood of fake tokens costs no query.
     Within it, each address still gets up to `RATE_LIMIT_PER_MINUTE` lookups a minute (ADR 0007).
   - The client is uvicorn's `scope["client"]`. Behind the proxy, uvicorn takes it from
     `X-Forwarded-For` only when the peer is in `--forwarded-allow-ips`; anyone else's header is
     ignored (tested with a real server), so a client cannot name itself a fresh bucket.
   - IPv6 clients count per /64 (a host usually holds a whole /64); IPv4-mapped addresses as IPv4.
   - At most 10 000 buckets, least recently seen dropped first: bounded memory.
   - **Every route counts, the probes included.** Exempting `/health` and `/ready` (the first
     version of this phase) let one address reach the app about 600 times a second in a local
     test, each `/ready` with its database queries, which also timed `/ready` out. A deploy
     script's retries and a monitor polling every minute stay well within the burst. `/health`
     is `async def`, so it costs no threadpool hop.
   - Refused requests are not counted, so retrying does not extend the wait.
   - Built in the lifespan and kept on `app.state`; the middleware reads it there, because settings
     are not available when the app object is created (import time needs no `DATABASE_URL`).
3. **App logs**: the lifespan gives the `transaction_analytics` logger one stdout handler,
   `%(asctime)s %(levelname)s %(name)s %(message)s`, at `LOG_LEVEL` (INFO). The root logger is
   left alone and records still propagate. `sqlalchemy.engine` is pinned to WARNING: at INFO it
   logs statement parameters. Plain text, not JSON: journald reads it, nothing parses it yet.
4. **Database limits for the app's engine only**: `statement_timeout` 30 s, sent as a server
   setting when connecting (PostgreSQL cancels the statement and the connection stays usable; a
   cancelled statement is a 500 like any database error), and a 5 s connect timeout. Milliseconds
   are rounded up, because 0 means "no limit". Migrations and the CLI create engines without them:
   an index build may take long.
5. **Migrations run before the app starts, never inside it**: `alembic upgrade head` as the systemd
   unit's `ExecStartPre` (README, "Running on a server"). One process cannot race itself, and the
   `fileConfig` concern of ADR 0003 cannot arise. New indexes on big tables use
   `CREATE INDEX CONCURRENTLY` in an `autocommit_block()`.
6. **TLS is the reverse proxy's job** (a Caddyfile example in the README). uvicorn binds 127.0.0.1,
   runs one worker (the rate limit is per process), stops within 30 s on shutdown
   (`--timeout-graceful-shutdown`; the default waits for open requests forever) and sends no
   `Server` header.

No new dependencies.

## Consequences

- A deploy is: stop, migrate, start, wait for `/ready`. The single process means a few seconds of
  downtime per deploy.
- Rolling the code back below a migration needs `alembic downgrade <revision>` first, or `/ready`
  stays 503.
- Every route, the probes included, can answer 429; clients should honour `Retry-After`.
- Paging a 50 000-row account at 500 per page takes 100 requests: the burst, then two a second.
- The app's ERROR lines (unhandled exceptions) are now on stdout with time and logger; before, they
  were bare lines on stderr.
- A statement running longer than 30 s fails. The largest import (50 000 rows) took about 4 s.

## Known limitations

- The rate limit lives in one process's memory: it resets on restart, and more workers or hosts
  would each allow the full rate. Several processes need a shared store (Redis) or the proxy's
  limit.
- The limit caps what a flood reaches, not what it costs: each refused request is still parsed
  and answered by the one event loop. In a local test (load generator on the same machine), 4 000
  refused requests in 4 s from one address still slowed other requests from 4 ms to about 300 ms,
  as much as the same flood did before it was limited. Shedding load before the app needs the
  proxy or the firewall (connection and request limits there).
- A client with more than 10 000 addresses (or /64s) gets fresh buckets: a distributed flood
  needs a limit in front of the server.
- A misconfigured `--forwarded-allow-ips` breaks the limit: `*` lets any client choose its
  address; a proxy that is not trusted puts all clients in the proxy's one bucket.
- There is no separate, stricter limit on failed token attempts; they share the general one.
- No zero-downtime deploys, no backups and no metrics: backups are the database operator's job
  (`pg_dump` or provider snapshots).
- `/docs` and `/openapi.json` stay public: they describe the API, not anyone's data.
- The systemd unit and Caddyfile in the README are examples, not run on a server by the suite; a
  unit test checks the unit's key directives (`tests/unit/test_readme_deploy.py`).

## Alternatives considered

- **Rate limit at the proxy only** (nginx `limit_req`): no code, but untested here, absent in
  development, and Caddy needs a plugin for it.
- **Shared counter in Redis or PostgreSQL**: survives restarts and works across processes, but adds
  a service, or turns every read into a write (rejected for `last_used_at` in ADR 0007).
- **`slowapi`**: a dependency with a decorator per route, still per process without Redis.
- **Rate limit per API token**: fairer behind NAT, but needs the database lookup the limit is
  meant to protect.
- **Probes exempt from the limit** (so a load balancer is never refused): an unlimited path into a
  single-process server. With no load balancer on one VPS, nothing needs the exemption.
- **`/ready` also when the database is newer than the code**: keeps a rolled-back app serving, but
  against a schema it does not know.
- **A container image and compose services**: considered and left out; one VPS runs the checkout
  with uv and systemd.
- **JSON logs** (`python-json-logger`): a dependency for a format nothing consumes yet.
