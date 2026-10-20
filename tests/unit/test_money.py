import random

import pytest

from transaction_analytics.domain.errors import FieldErrorCode, FieldValidationError
from transaction_analytics.domain.money import (
    MAX_ABS_AMOUNT_MINOR,
    Currency,
    format_minor,
    parse_amount_minor,
    parse_currency,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.30", 1230),
        ("-12.30", -1230),
        ("+12.30", 1230),
        ("12.3", 1230),
        ("12", 1200),
        ("0", 0),
        ("-0.00", 0),
        ("  -0.01 ", -1),
        ("007.50", 750),
    ],
)
def test_parse_amount_gbp(raw: str, expected: int) -> None:
    assert parse_amount_minor(raw, Currency.GBP) == expected


def test_zero_exponent_currency_has_no_fraction() -> None:
    assert parse_amount_minor("-1500", Currency.JPY) == -1500
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor("1500.5", Currency.JPY)
    assert exc.value.code is FieldErrorCode.AMOUNT_TOO_PRECISE


def test_extra_precision_is_rejected_not_rounded() -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor("1.005", Currency.GBP)
    assert exc.value.code is FieldErrorCode.AMOUNT_TOO_PRECISE


@pytest.mark.parametrize(
    "raw",
    ["1,000.00", "1e3", "NaN", "Infinity", "1_000", "£10", ".5", "5.", "--1", "1.2.3", "abc"],
)
def test_non_plain_decimals_are_rejected(raw: str) -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor(raw, Currency.GBP)
    assert exc.value.code is FieldErrorCode.INVALID_AMOUNT_FORMAT


def test_empty_amount_is_missing() -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor("   ", Currency.GBP)
    assert exc.value.code is FieldErrorCode.MISSING_VALUE


def test_bigint_boundary() -> None:
    max_major = f"{MAX_ABS_AMOUNT_MINOR // 100}.{MAX_ABS_AMOUNT_MINOR % 100:02d}"
    assert parse_amount_minor(max_major, Currency.GBP) == MAX_ABS_AMOUNT_MINOR
    assert parse_amount_minor(f"-{max_major}", Currency.GBP) == -MAX_ABS_AMOUNT_MINOR


@pytest.mark.parametrize("raw", ["92233720368547758.08", "99999999999999999999999999999.99"])
def test_huge_amounts_are_out_of_range(raw: str) -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor(raw, Currency.GBP)
    assert exc.value.code is FieldErrorCode.AMOUNT_OUT_OF_RANGE


def test_absurdly_long_amount_text_is_rejected_cheaply() -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor("9" * 10_000, Currency.GBP)
    assert exc.value.code is FieldErrorCode.INVALID_AMOUNT_FORMAT


def test_error_message_does_not_echo_raw_value() -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_amount_minor("SECRET-123", Currency.GBP)
    assert "SECRET" not in exc.value.message


@pytest.mark.parametrize(("raw", "expected"), [("GBP", Currency.GBP), (" eur ", Currency.EUR)])
def test_parse_currency(raw: str, expected: Currency) -> None:
    assert parse_currency(raw) is expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [("", FieldErrorCode.MISSING_VALUE), ("XYZ", FieldErrorCode.UNSUPPORTED_CURRENCY)],
)
def test_parse_currency_errors(raw: str, code: FieldErrorCode) -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_currency(raw)
    assert exc.value.code is code


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [
        (-1230, Currency.GBP, "-12.30"),
        (1230, Currency.GBP, "12.30"),
        (5, Currency.GBP, "0.05"),
        (-5, Currency.GBP, "-0.05"),
        (50, Currency.EUR, "0.50"),
        (0, Currency.USD, "0.00"),
        (1500, Currency.JPY, "1500"),
        (-1500, Currency.JPY, "-1500"),
        (0, Currency.JPY, "0"),
        (MAX_ABS_AMOUNT_MINOR, Currency.GBP, "92233720368547758.07"),
        (-MAX_ABS_AMOUNT_MINOR, Currency.GBP, "-92233720368547758.07"),
        # Sums can exceed BIGINT (two maximal amounts); formatting stays exact.
        (2 * MAX_ABS_AMOUNT_MINOR, Currency.USD, "184467440737095516.14"),
        (-2 * MAX_ABS_AMOUNT_MINOR, Currency.JPY, "-18446744073709551614"),
    ],
)
def test_format_minor(minor: int, currency: Currency, expected: str) -> None:
    assert format_minor(minor, currency) == expected


@pytest.mark.parametrize("currency", list(Currency))
def test_format_minor_is_the_inverse_of_parse(currency: Currency) -> None:
    generator = random.Random(20261020)  # fixed seed: a failure is reproducible
    samples = [0, 1, -1, 9, 10, 99, 100, 101, MAX_ABS_AMOUNT_MINOR, -MAX_ABS_AMOUNT_MINOR]
    samples += [generator.randint(-MAX_ABS_AMOUNT_MINOR, MAX_ABS_AMOUNT_MINOR) for _ in range(500)]
    samples += [generator.randint(-10_000, 10_000) for _ in range(500)]

    for minor in samples:
        assert parse_amount_minor(format_minor(minor, currency), currency) == minor, minor
