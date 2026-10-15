"""Parser for the canonical statement CSV format (v1).

Format (see README):
- UTF-8, optional BOM. First line is a header.
- Required columns (any order, case-insensitive): date, description, amount, currency.
- Extra columns are allowed and ignored.
- `amount` is signed: negative = debit, positive = credit.

The parser never stops at the first bad row. It returns every valid transaction plus a
row-level error for every invalid one. Whole-file problems raise FileRejectedError.
Deciding whether an import with many row errors should fail is the caller's job (Phase 3).
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from transaction_analytics.domain.errors import (
    FieldErrorCode,
    FieldValidationError,
    FileErrorCode,
    FileRejectedError,
)
from transaction_analytics.domain.fields import (
    clean_description,
    normalize_description,
    parse_booking_date,
)
from transaction_analytics.domain.money import Currency, parse_amount_minor, parse_currency

REQUIRED_COLUMNS: tuple[str, ...] = ("date", "description", "amount", "currency")

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_ROWS = 50_000


@dataclass(frozen=True, slots=True)
class ParsedTransaction:
    row_number: int
    booked_on: date
    description: str
    normalized_description: str
    amount_minor: int
    currency: Currency


@dataclass(frozen=True, slots=True)
class RowError:
    row_number: int
    code: FieldErrorCode
    column: str | None
    message: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    transactions: list[ParsedTransaction] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return len(self.transactions) + len(self.errors)


def parse_statement_csv(
    content: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ParseResult:
    """Parse raw file bytes. Row numbers count the header as row 1, like a spreadsheet."""
    if len(content) > max_bytes:
        raise FileRejectedError(
            FileErrorCode.FILE_TOO_LARGE, f"File is larger than {max_bytes} bytes."
        )
    text = _decode(content)
    if not text.strip():
        raise FileRejectedError(FileErrorCode.EMPTY_FILE, "File is empty.")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
        column_index = _resolve_columns(header)
        result = ParseResult()
        for record_index, record in enumerate(reader, start=2):
            if not any(cell.strip() for cell in record):
                continue  # blank line
            if len(result.transactions) + len(result.errors) >= max_rows:
                raise FileRejectedError(
                    FileErrorCode.TOO_MANY_ROWS, f"File has more than {max_rows} data rows."
                )
            _parse_record(record, record_index, len(header), column_index, result)
    except csv.Error:
        raise FileRejectedError(
            FileErrorCode.MALFORMED_CSV, "File is not valid CSV (check quoting)."
        ) from None

    if result.total_rows == 0:
        raise FileRejectedError(FileErrorCode.NO_DATA_ROWS, "File has a header but no data rows.")
    return result


def _decode(content: bytes) -> str:
    try:
        # utf-8-sig strips a leading BOM, which Excel adds to "CSV UTF-8" exports.
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileRejectedError(
            FileErrorCode.INVALID_ENCODING, "File must be UTF-8 encoded."
        ) from None


def _resolve_columns(header: list[str]) -> dict[str, int]:
    names = [name.strip().casefold() for name in header]
    if len(set(names)) != len(names):
        raise FileRejectedError(
            FileErrorCode.DUPLICATE_COLUMNS, "Header contains duplicate column names."
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in names]
    if missing:
        raise FileRejectedError(
            FileErrorCode.MISSING_COLUMNS,
            f"Header is missing required columns: {', '.join(missing)}.",
        )
    return {column: names.index(column) for column in REQUIRED_COLUMNS}


def _parse_record(
    record: list[str],
    row_number: int,
    expected_width: int,
    column_index: dict[str, int],
    result: ParseResult,
) -> None:
    if len(record) != expected_width:
        result.errors.append(
            RowError(
                row_number,
                FieldErrorCode.COLUMN_COUNT_MISMATCH,
                None,
                f"Row has {len(record)} values but the header has {expected_width} columns.",
            )
        )
        return

    def value(column: str) -> str:
        return record[column_index[column]]

    column = "currency"
    try:
        currency = parse_currency(value(column))
        column = "amount"
        amount_minor = parse_amount_minor(value(column), currency)
        column = "date"
        booked_on = parse_booking_date(value(column))
        column = "description"
        description = clean_description(value(column))
    except FieldValidationError as exc:
        result.errors.append(RowError(row_number, exc.code, column, exc.message))
        return

    result.transactions.append(
        ParsedTransaction(
            row_number=row_number,
            booked_on=booked_on,
            description=description,
            normalized_description=normalize_description(description),
            amount_minor=amount_minor,
            currency=currency,
        )
    )
