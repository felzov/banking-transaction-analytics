"""API tokens (ADR 0007).

A token is "bta_" plus 43 base64url characters (32 random bytes). The prefix makes a leaked token
recognisable to secret scanners, and a header that does not have this shape is refused before any
database query. Only the SHA-256 of a token is stored: with 256 bits of entropy a fast hash cannot
be reversed by guessing, while a slow password hash would cost CPU on every request.

Generating a token needs randomness, so it lives in services/auth.py; this module stays pure.
"""

import hashlib
import re

from transaction_analytics.domain.errors import TokenErrorCode, TokenValidationError

TOKEN_PREFIX = "bta_"
TOKEN_RANDOM_BYTES = 32
_TOKEN = re.compile(r"bta_[A-Za-z0-9_-]{43}")

DEFAULT_TOKEN_DAYS = 90
MAX_TOKEN_DAYS = 365


def is_well_formed(token: str) -> bool:
    return _TOKEN.fullmatch(token) is not None


def token_digest(token: str) -> bytes:
    """What is stored and looked up instead of a (well-formed) token."""
    return hashlib.sha256(token.encode("ascii")).digest()


def check_token_days(days: int) -> int:
    """Tokens always expire: a forgotten one stops working on its own."""
    if not 1 <= days <= MAX_TOKEN_DAYS:
        raise TokenValidationError(
            TokenErrorCode.LIFETIME_OUT_OF_RANGE,
            f"Token lifetime must be between 1 and {MAX_TOKEN_DAYS} days.",
        )
    return days
