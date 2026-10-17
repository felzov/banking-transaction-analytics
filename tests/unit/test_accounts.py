import pytest

from transaction_analytics.domain.accounts import MAX_ACCOUNT_NAME_LENGTH, clean_account_name
from transaction_analytics.domain.errors import AccountErrorCode, AccountValidationError


def test_whitespace_is_collapsed_and_trimmed_case_kept() -> None:
    assert clean_account_name("  Joint \t\n Account ") == "Joint Account"


def test_no_break_space_is_whitespace() -> None:
    # The database CHECK cannot see a no-break space portably; the domain must.
    assert clean_account_name("\u00a0Savings\u00a0\u00a0Pot\u00a0") == "Savings Pot"


def test_control_characters_that_are_whitespace_become_spaces() -> None:
    # NEL (U+0085) is Cc but str.isspace(); like tabs and newlines it is collapsed, not rejected.
    assert clean_account_name("Joint\x85Account") == "Joint Account"


def test_nfkc_is_applied() -> None:
    assert clean_account_name("\ufb01nance") == "finance"  # "fi" ligature


def test_format_characters_are_kept() -> None:
    name = "Family \U0001f468\u200d\U0001f469 \u200fRTL"
    assert clean_account_name(name) == name


def test_exactly_the_maximum_length_is_accepted() -> None:
    assert clean_account_name("x" * MAX_ACCOUNT_NAME_LENGTH) == "x" * MAX_ACCOUNT_NAME_LENGTH


def test_length_is_measured_after_normalization() -> None:
    padded = "  " + "x" * MAX_ACCOUNT_NAME_LENGTH + "  "
    assert clean_account_name(padded) == "x" * MAX_ACCOUNT_NAME_LENGTH


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", AccountErrorCode.NAME_REQUIRED),
        ("   ", AccountErrorCode.NAME_REQUIRED),
        ("\u00a0\t\u2003\n", AccountErrorCode.NAME_REQUIRED),
        # Only invisible characters: zero-width space, BOM, word joiner, bidi override,
        # zero-width joiner, a lone combining mark.
        ("\u200b", AccountErrorCode.NAME_REQUIRED),
        ("\ufeff", AccountErrorCode.NAME_REQUIRED),
        ("\u2060", AccountErrorCode.NAME_REQUIRED),
        ("\u202e", AccountErrorCode.NAME_REQUIRED),
        ("\u200b \u200d", AccountErrorCode.NAME_REQUIRED),
        ("\u0301", AccountErrorCode.NAME_REQUIRED),
        ("x" * (MAX_ACCOUNT_NAME_LENGTH + 1), AccountErrorCode.NAME_TOO_LONG),
        ("Private\x00Account", AccountErrorCode.NAME_INVALID_CHARACTERS),
        ("Private\x07Account", AccountErrorCode.NAME_INVALID_CHARACTERS),
    ],
)
def test_invalid_names(raw: str, code: AccountErrorCode) -> None:
    with pytest.raises(AccountValidationError) as exc:
        clean_account_name(raw)

    assert exc.value.code is code
    assert "Private" not in exc.value.message
