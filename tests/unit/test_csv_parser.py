from datetime import date
from pathlib import Path

import pytest

from transaction_analytics.domain.csv_parser import parse_statement_csv
from transaction_analytics.domain.errors import FieldErrorCode, FileErrorCode, FileRejectedError
from transaction_analytics.domain.money import Currency

FIXTURES = Path(__file__).parent.parent / "fixtures"
HEADER = b"date,description,amount,currency\n"


def test_valid_statement_parses_every_row() -> None:
    result = parse_statement_csv((FIXTURES / "statement_valid.csv").read_bytes())

    assert result.errors == []
    assert [t.amount_minor for t in result.transactions] == [250000, -320, -320, -4567, -12050]
    assert [t.currency for t in result.transactions][-1] is Currency.EUR
    first = result.transactions[0]
    assert first.row_number == 2
    assert first.booked_on == date(2026, 9, 1)


def test_same_looking_rows_are_both_kept() -> None:
    # Two identical coffees on one day are two real purchases. Dedup is NOT the parser's job.
    result = parse_statement_csv((FIXTURES / "statement_valid.csv").read_bytes())

    coffees = [t for t in result.transactions if t.normalized_description == "coffee shop"]
    assert len(coffees) == 2
    assert coffees[0].description == "Coffee Shop"  # whitespace cleaned, case kept


def test_parsing_is_deterministic() -> None:
    content = (FIXTURES / "statement_valid.csv").read_bytes()
    assert parse_statement_csv(content) == parse_statement_csv(content)


def test_invalid_rows_produce_errors_without_stopping_import() -> None:
    result = parse_statement_csv((FIXTURES / "statement_mixed_errors.csv").read_bytes())

    assert [t.row_number for t in result.transactions] == [2, 9]
    assert [(e.row_number, e.code, e.column) for e in result.errors] == [
        (3, FieldErrorCode.INVALID_DATE, "date"),
        (4, FieldErrorCode.AMOUNT_TOO_PRECISE, "amount"),
        (5, FieldErrorCode.INVALID_AMOUNT_FORMAT, "amount"),
        (6, FieldErrorCode.UNSUPPORTED_CURRENCY, "currency"),
        (7, FieldErrorCode.MISSING_VALUE, "description"),
        (8, FieldErrorCode.COLUMN_COUNT_MISMATCH, None),
    ]
    assert result.total_rows == 8


def test_utf8_bom_is_accepted() -> None:
    content = "﻿date,description,amount,currency\n2026-09-01,Café,-1.00,GBP\n".encode()

    result = parse_statement_csv(content)

    assert result.transactions[0].description == "Café"


def test_columns_in_any_order_case_and_extras() -> None:
    content = b"Currency, AMOUNT ,Notes,Description,Date\nGBP,-1.00,ignored,Bus,2026-09-01\n"

    result = parse_statement_csv(content)

    assert result.transactions[0].amount_minor == -100
    assert result.transactions[0].description == "Bus"


def test_crlf_and_blank_lines_are_handled() -> None:
    content = b"date,description,amount,currency\r\n\r\n2026-09-01,Bus,-1.00,GBP\r\n\r\n"

    result = parse_statement_csv(content)

    assert len(result.transactions) == 1
    assert result.transactions[0].row_number == 3


def test_huge_amount_is_a_row_error_not_a_crash() -> None:
    content = HEADER + b"2026-09-01,Big,99999999999999999999.00,GBP\n2026-09-01,Ok,-1.00,GBP\n"

    result = parse_statement_csv(content)

    assert result.errors[0].code is FieldErrorCode.AMOUNT_OUT_OF_RANGE
    assert len(result.transactions) == 1


def test_row_error_messages_do_not_leak_values() -> None:
    content = HEADER + b"2026-09-01,ACCOUNT 12345678,LEAK-ME,GBP\n"

    result = parse_statement_csv(content)

    assert "LEAK-ME" not in result.errors[0].message
    assert "12345678" not in result.errors[0].message


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", FileErrorCode.EMPTY_FILE),
        (b"  \n\n", FileErrorCode.EMPTY_FILE),
        (HEADER, FileErrorCode.NO_DATA_ROWS),
        (b"date,description,amount\n2026-09-01,x,1.00\n", FileErrorCode.MISSING_COLUMNS),
        (b"date,Date,description,amount,currency\n", FileErrorCode.DUPLICATE_COLUMNS),
        (
            "date,description,amount,currency\n2026-09-01,Café,1,GBP\n".encode("cp1252"),
            FileErrorCode.INVALID_ENCODING,
        ),
        (HEADER + b'2026-09-01,"unterminated,1.00,GBP\n', FileErrorCode.MALFORMED_CSV),
    ],
)
def test_whole_file_rejections(content: bytes, code: FileErrorCode) -> None:
    with pytest.raises(FileRejectedError) as exc:
        parse_statement_csv(content)
    assert exc.value.code is code


def test_file_size_limit() -> None:
    with pytest.raises(FileRejectedError) as exc:
        parse_statement_csv(HEADER + b"x" * 100, max_bytes=50)
    assert exc.value.code is FileErrorCode.FILE_TOO_LARGE


def test_row_count_limit_counts_invalid_rows_too() -> None:
    content = HEADER + b"2026-09-01,a,1.00,GBP\n" + b"bad,b,1.00,GBP\n" + b"2026-09-01,c,1.00,GBP\n"

    assert parse_statement_csv(content, max_rows=3).total_rows == 3
    with pytest.raises(FileRejectedError) as exc:
        parse_statement_csv(content, max_rows=2)
    assert exc.value.code is FileErrorCode.TOO_MANY_ROWS


def test_nul_in_description_is_a_row_error() -> None:
    content = HEADER + b"2026-09-01,Caf\x00e,-1.00,GBP\n2026-09-01,Ok,-1.00,GBP\n"

    result = parse_statement_csv(content)

    assert [(e.row_number, e.code, e.column) for e in result.errors] == [
        (2, FieldErrorCode.INVALID_CHARACTERS, "description")
    ]
    assert [t.row_number for t in result.transactions] == [3]
