"""Account name rules.

The database CHECK (migration 0002) repeats the parts SQL can express deterministically: length,
no leading/trailing spaces, no control characters. Unicode whitespace such as a no-break space
depends on the database locale in SQL regexes, so only this function guarantees it is handled.
"""

import re
import unicodedata

from transaction_analytics.domain.errors import AccountErrorCode, AccountValidationError

MAX_ACCOUNT_NAME_LENGTH = 100

_WHITESPACE = re.compile(r"\s+")

# Letters, numbers, punctuation, symbols. Format characters (zero-width space, joiners, bidi
# marks), lone combining marks and separators render as nothing on their own.
_VISIBLE_CATEGORIES = ("L", "N", "P", "S")


def clean_account_name(raw: str) -> str:
    """Unicode-normalized, whitespace collapsed and trimmed, case kept.

    A name must contain at least one visible character; format characters next to visible text
    (emoji joiners, RTL marks) are kept.
    """
    name = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", raw)).strip()
    if not any(unicodedata.category(char).startswith(_VISIBLE_CATEGORIES) for char in name):
        raise AccountValidationError(AccountErrorCode.NAME_REQUIRED, "Account name is required.")
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise AccountValidationError(
            AccountErrorCode.NAME_INVALID_CHARACTERS, "Account name contains control characters."
        )
    if len(name) > MAX_ACCOUNT_NAME_LENGTH:
        raise AccountValidationError(
            AccountErrorCode.NAME_TOO_LONG,
            f"Account name is longer than {MAX_ACCOUNT_NAME_LENGTH} characters.",
        )
    return name
