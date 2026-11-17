# Architecture decisions

Back to the [README](../../README.md).

- [ADR 0001 — Money: signed minor units, no rounding](0001-money-signed-minor-units.md)
- [ADR 0002 — CSV parsing: strict format, row-level errors](0002-csv-parsing-policy.md)
- [ADR 0003 — Persistence: ownership in the schema, CHECK over ENUM, isolated test databases](0003-persistence-schema.md)
- [ADR 0004 — Import pipeline: synchronous, one transaction, partial import](0004-import-pipeline.md)
- [ADR 0005 — API conventions: error envelope, no echoed values, placeholder owner](0005-api-conventions.md)
- [ADR 0006 — Deduplication: exact keys, multiset union, enforced by a unique constraint](0006-deduplication.md)
- [ADR 0007 — Authentication: opaque API tokens, stored hashed, issued by a CLI](0007-authentication.md)
- [ADR 0008 — Analytics: per-account, per-currency SQL totals; money in API responses](0008-analytics.md)
- [ADR 0009 — Ledger source: accounts fed by banking-ledger-api, pulled per request](0009-ledger-source.md)
- [ADR 0010 — Operational readiness: probes, in-process rate limit, app logs, database timeouts](0010-operational-readiness.md)
