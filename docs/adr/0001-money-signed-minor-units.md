# ADR 0001 — Money: signed minor units, no rounding

- Status: Accepted
- Date: 2026-10-15

## Context

Statement amounts arrive as text. Floats cannot represent most decimal amounts exactly
(`0.1 + 0.2 != 0.3`), and silently rounding a bank amount changes financial data.
Banks also disagree on sign conventions and on whether debits and credits share a column.

## Decision

1. Store every amount as a signed integer in the currency's minor unit
   (`-12.30 GBP` becomes `-1230`). Persist as PostgreSQL `BIGINT`.
2. Sign: negative = money leaving the account (debit), positive = money arriving (credit).
   This matches the owner's perspective and the convention in banking-ledger-api.
3. Each supported currency has an explicit minor-unit exponent (GBP/EUR/USD = 2, JPY = 0).
4. Parsing is strict and exact:
   - Only plain decimals are accepted (`[+-]?digits[.digits]`).
   - More fractional digits than the exponent allows is a row error. Nothing is rounded.
   - Conversion uses integer arithmetic on the digit string, not float and not a
     precision-limited `Decimal` context.
   - Values outside the BIGINT range are a row error, raised before the database is involved.
5. Currency is always stored next to the amount. Sums are computed per currency; amounts in
   different currencies are never added together. There is no FX conversion.

## Consequences

- Exact, comparable amounts; deduplication can compare integers.
- Formats with thousands separators, currency symbols or separate debit/credit columns need an
  adapter that converts them into this canonical form first.
- Adding a currency means adding its exponent; an unknown currency is a row error, not a guess.

## Alternatives considered

- `NUMERIC(19, 4)` with `Decimal`: exact, but invites "how many decimals?" ambiguity and makes
  rounding easy to introduce by accident.
- Float: rejected; inexact.
