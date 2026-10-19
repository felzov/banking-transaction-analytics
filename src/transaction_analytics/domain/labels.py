"""Names people give things: accounts, users and API tokens follow one rule.

The database CHECKs (migrations 0002 and 0004) repeat the parts SQL can express deterministically:
length, no leading/trailing spaces, no control characters. Unicode whitespace such as a no-break
space depends on the database locale in SQL regexes, so only this function guarantees it is handled.
"""

import re
import unicodedata

from transaction_analytics.domain.errors import LabelErrorCode, LabelValidationError

MAX_LABEL_LENGTH = 100

_WHITESPACE = re.compile(r"\s+")

# Letters, numbers, punctuation, symbols. Format characters (zero-width space, joiners, bidi
# marks), lone combining marks and separators render as nothing on their own.
_VISIBLE_CATEGORIES = ("L", "N", "P", "S")


def clean_label(raw: str) -> str:
    """Unicode-normalized, whitespace collapsed and trimmed, case kept.

    A name must contain at least one visible character; format characters next to visible text
    (emoji joiners, RTL marks) are kept.
    """
    name = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", raw)).strip()
    if not any(unicodedata.category(char).startswith(_VISIBLE_CATEGORIES) for char in name):
        raise LabelValidationError(LabelErrorCode.REQUIRED, "Name is required.")
    if any(unicodedata.category(char) == "Cc" for char in name):
        raise LabelValidationError(
            LabelErrorCode.INVALID_CHARACTERS, "Name contains control characters."
        )
    if len(name) > MAX_LABEL_LENGTH:
        raise LabelValidationError(
            LabelErrorCode.TOO_LONG, f"Name is longer than {MAX_LABEL_LENGTH} characters."
        )
    return name
