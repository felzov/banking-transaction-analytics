"""The shared name rule. Its details are covered through accounts in test_accounts.py."""

import pytest

from transaction_analytics.domain.errors import LabelErrorCode, LabelValidationError
from transaction_analytics.domain.labels import MAX_LABEL_LENGTH, clean_label


def test_name_is_normalized_like_an_account_name() -> None:
    assert clean_label("  my\u00a0\tlaptop ") == "my laptop"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", LabelErrorCode.REQUIRED),
        ("\u200b", LabelErrorCode.REQUIRED),
        ("x" * (MAX_LABEL_LENGTH + 1), LabelErrorCode.TOO_LONG),
        ("lap\x00top", LabelErrorCode.INVALID_CHARACTERS),
    ],
)
def test_invalid_names_have_generic_messages(raw: str, code: LabelErrorCode) -> None:
    with pytest.raises(LabelValidationError) as exc:
        clean_label(raw)

    assert exc.value.code is code
    assert "Account" not in exc.value.message
    if raw.strip():
        assert raw not in exc.value.message
