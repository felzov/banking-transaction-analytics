# Supported CSV format (v1)

The one CSV dialect imports accept. Back to the [README](../README.md).

| Rule | Behaviour |
|---|---|
| Encoding | UTF-8; a leading BOM (Excel "CSV UTF-8") is accepted. Anything else rejects the file. |
| Header | Required. Columns `date`, `description`, `amount`, `currency` in any order, case-insensitive. Extra columns are ignored. Duplicate names reject the file. |
| `date` | `YYYY-MM-DD` only, must be a real calendar date. |
| `amount` | Plain signed decimal: `-12.30`, `2500`. **Negative = money out (debit), positive = money in (credit).** No currency symbols, thousands separators or exponent notation. More decimal places than the currency allows is an error; nothing is rounded. |
| `currency` | ISO 4217 code: GBP, EUR, USD, JPY (case-insensitive). |
| `description` | Required, max 500 characters after whitespace is collapsed. Control characters (e.g. NUL) are a row error (`invalid_characters`); emoji and RTL marks are kept. |
| Blank lines | Skipped. |
| Limits | 5 MiB per file (larger uploads get 413), 50 000 data rows. Settings `IMPORT_MAX_BYTES`, `IMPORT_MAX_ROWS`. |

Row numbers in errors count the header as row 1, like a spreadsheet. A quoted value that spans
several lines still counts as one row.

Example:

```csv
date,description,amount,currency
2026-09-01,SALARY ACME LTD,2500.00,GBP
2026-09-02,Coffee Shop,-3.20,GBP
```

### Errors

- **Row errors** (`invalid_date`, `amount_too_precise`, `unsupported_currency`, ...) do not stop
  the import. Every valid row is still returned.
- **File errors** (`empty_file`, `invalid_encoding`, `missing_columns`, `malformed_csv`,
  `too_many_rows`, ...) reject the whole file.
- Error messages never contain values from the file, because statements contain personal
  financial data and errors end up in responses and logs.

Codes are defined in `src/transaction_analytics/domain/errors.py`.
