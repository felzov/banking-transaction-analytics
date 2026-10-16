from datetime import date

import pytest

from transaction_analytics.domain.errors import FieldErrorCode, FieldValidationError
from transaction_analytics.domain.fields import (
    MAX_DESCRIPTION_LENGTH,
    clean_description,
    normalize_description,
    parse_booking_date,
)


def test_parse_valid_date() -> None:
    assert parse_booking_date(" 2026-02-28 ") == date(2026, 2, 28)


@pytest.mark.parametrize(
    "raw",
    ["2026-02-30", "2026-13-01", "01/09/2026", "20260901", "2026-W36-1", "2026-9-1"],
)
def test_invalid_dates_are_rejected(raw: str) -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_booking_date(raw)
    assert exc.value.code is FieldErrorCode.INVALID_DATE


def test_missing_date() -> None:
    with pytest.raises(FieldValidationError) as exc:
        parse_booking_date("")
    assert exc.value.code is FieldErrorCode.MISSING_VALUE


def test_clean_description_collapses_whitespace_and_keeps_case() -> None:
    assert clean_description("  Coffee \t  Shop\n ") == "Coffee Shop"


def test_clean_description_applies_nfkc() -> None:
    # Full-width letters (U+FF34...) and a no-break space (U+00A0) appear in exported statements.
    fullwidth_tesco = "\uff34\uff25\uff33\uff23\uff2f"
    assert clean_description(f"{fullwidth_tesco}\u00a0Store") == "TESCO Store"


def test_normalized_description_ignores_case() -> None:
    assert normalize_description(clean_description("Coffee SHOP")) == normalize_description(
        clean_description("coffee shop")
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("   ", FieldErrorCode.MISSING_VALUE),
        ("x" * (MAX_DESCRIPTION_LENGTH + 1), FieldErrorCode.DESCRIPTION_TOO_LONG),
    ],
)
def test_description_errors(raw: str, code: FieldErrorCode) -> None:
    with pytest.raises(FieldValidationError) as exc:
        clean_description(raw)
    assert exc.value.code is code


@pytest.mark.parametrize("raw", ["Caf\x00e", "Bus\x01fare", "Shop\x7f"])
def test_control_characters_are_rejected(raw: str) -> None:
    # NUL cannot be stored in PostgreSQL TEXT; other Cc characters are invisible junk.
    with pytest.raises(FieldValidationError) as exc:
        clean_description(raw)
    assert exc.value.code is FieldErrorCode.INVALID_CHARACTERS
    assert raw not in exc.value.message


def test_format_characters_are_kept() -> None:
    # Cf (ZWJ in emoji sequences, RTL marks) is legitimate text, not a control character.
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    assert clean_description(f"Gift {family}") == f"Gift {family}"
    assert clean_description("‏شركة") == "‏شركة"


def test_whitespace_controls_are_still_collapsed_not_rejected() -> None:
    # \x85 (NEL) and \x1c-\x1f are Cc too, but Python treats them as whitespace.
    assert clean_description("Coffee\t\r\nShop\x0b\x0c\x85\x1f") == "Coffee Shop"
