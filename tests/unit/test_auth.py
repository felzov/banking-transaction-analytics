import hashlib

import pytest

from transaction_analytics.domain.auth import (
    MAX_TOKEN_DAYS,
    TOKEN_PREFIX,
    check_token_days,
    is_well_formed,
    token_digest,
)
from transaction_analytics.domain.errors import TokenErrorCode, TokenValidationError
from transaction_analytics.services.auth import IssuedToken, generate_token

TOKEN = "bta_" + "Ab9_-" * 8 + "xyz"  # 43 characters after the prefix


def test_generated_tokens_are_well_formed_and_distinct() -> None:
    tokens = {generate_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(token.startswith(TOKEN_PREFIX) and is_well_formed(token) for token in tokens)


def test_a_well_formed_token() -> None:
    assert is_well_formed(TOKEN)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "bta_",
        TOKEN[:-1],  # 42 characters
        TOKEN + "A",  # 44
        "btb_" + TOKEN[4:],  # wrong prefix
        "BTA_" + TOKEN[4:],
        TOKEN[:-1] + "=",  # padding
        TOKEN[:-1] + "+",  # standard base64, not base64url
        TOKEN[:-1] + "é",
        TOKEN + "\n",
        " " + TOKEN,
    ],
)
def test_malformed_tokens(token: str) -> None:
    assert not is_well_formed(token)


def test_digest_is_the_sha256_of_the_token_text() -> None:
    # Pinned: changing the digest makes every stored token unusable.
    assert token_digest(TOKEN) == hashlib.sha256(TOKEN.encode("ascii")).digest()
    assert len(token_digest(TOKEN)) == 32


@pytest.mark.parametrize("days", [1, 90, MAX_TOKEN_DAYS])
def test_token_lifetime_in_range(days: int) -> None:
    assert check_token_days(days) == days


@pytest.mark.parametrize("days", [0, -1, MAX_TOKEN_DAYS + 1])
def test_token_lifetime_out_of_range(days: int) -> None:
    with pytest.raises(TokenValidationError) as exc:
        check_token_days(days)

    assert exc.value.code is TokenErrorCode.LIFETIME_OUT_OF_RANGE
    assert str(days) not in exc.value.message


def test_issued_token_repr_hides_the_token() -> None:
    issued = IssuedToken(token=None, plaintext=TOKEN)  # type: ignore[arg-type]

    assert TOKEN not in repr(issued)
