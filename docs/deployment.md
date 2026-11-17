# Deployment

Running on one server (ADR 0010). Back to the [README](../README.md).

One host (a VPS), one app process, behind a reverse proxy that terminates TLS. No container image:
the server runs a checkout with its virtualenv, under systemd. PostgreSQL 17 runs on the same host
or as a managed service; backups are its operator's job.

```bash
# As the service user, in /opt/banking-transaction-analytics (a checkout of a release)
uv sync --locked --no-dev        # .venv with the locked runtime dependencies only
```

Configuration comes from the environment: put `DATABASE_URL` and any settings below in
`/etc/transaction-analytics.env` (owner root, mode 600), not in a `.env` in the checkout.

```ini
# /etc/systemd/system/transaction-analytics.service
[Unit]
Description=Banking Transaction Analytics
# postgresql.service orders the start after a PostgreSQL on this host; without one it is ignored.
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
User=analytics
WorkingDirectory=/opt/banking-transaction-analytics
EnvironmentFile=/etc/transaction-analytics.env
# Migrations run before every start, as their own process, never inside the app.
ExecStartPre=/opt/banking-transaction-analytics/.venv/bin/alembic upgrade head
ExecStart=/opt/banking-transaction-analytics/.venv/bin/uvicorn transaction_analytics.main:app \
    --host 127.0.0.1 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 \
    --timeout-graceful-shutdown 30 --no-server-header
Restart=on-failure
# Not systemd's 100 ms: five quick failures (e.g. the database still starting) would hit its start
# limit and leave the service down.
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```text
# /etc/caddy/Caddyfile: TLS certificates are obtained and renewed automatically
api.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Deploy a new version: update the checkout, `uv sync --locked --no-dev`, then
`sudo systemctl restart transaction-analytics` (stop, migrate, start; a few seconds of downtime) and
wait until it is ready: `curl --fail --retry 15 --retry-all-errors --retry-delay 1 localhost:8000/ready`.

- **`--workers 1`**: the rate limit is kept in the process's memory; more workers would each allow
  the full rate.
- **`--forwarded-allow-ips`** must name the proxy, and only the proxy. uvicorn then takes the
  client's address from `X-Forwarded-For`, which Caddy sets itself (it ignores what clients send
  unless told to trust them). `*` would let any client choose its address and escape the rate
  limit; a proxy left out would put every client into one shared limit.
- **Probes**: `/health` is liveness (the process answers; never touches the database). `/ready` is
  200 only when the database answers within 2 s and is migrated to exactly this code's revision;
  otherwise 503 `not_ready`, with the reason in the log. Point deploy scripts and uptime monitors
  at `/ready`. Both probes count against the rate limit like any request: the deploy command
  below and a monitor polling every minute stay well within it. Rolling the code back below a migration needs
  `.venv/bin/alembic downgrade <revision>` first, or `/ready` stays 503.
- **Logs** go to the journal (`journalctl -u transaction-analytics`): uvicorn's own lines and
  access log, and the app's as `<time> <LEVEL> <logger> <message>`. Neither contains amounts or
  descriptions.
- **New migrations**: an index on a big table is built with `CREATE INDEX CONCURRENTLY` inside
  `with op.get_context().autocommit_block():`, so it does not block writes while it builds.

| Setting | Default | |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | 120 | Requests per minute per client address (IPv6: per /64); `0` turns the limit off |
| `RATE_LIMIT_BURST` | 30 | Requests a client may send at once before the per-minute rate applies |
| `DB_STATEMENT_TIMEOUT_SECONDS` | 30 | PostgreSQL cancels an app statement that runs longer (a 500). Migrations and the CLI have no limit |
| `DB_CONNECT_TIMEOUT_SECONDS` | 5 | Per new database connection |
| `LOG_LEVEL` | INFO | The app's own loggers: DEBUG, INFO, WARNING or ERROR |
