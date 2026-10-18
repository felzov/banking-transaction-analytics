"""Deduplication keys for imported transactions (ADR 0006).

Two rows are the same transaction when they have the same date, amount, currency and normalized
description. Identical rows inside one file are still different transactions (two coffees on the
same day, ADR 0002), so each row also gets its occurrence: 1 for the first row with its key in the
file, 2 for the second, and so on. The database keeps (account, fingerprint, occurrence) unique and
skips rows that already exist, so importing a key n times into an account that holds it m times
adds max(0, n - m) rows.

The fingerprint is a hash because the normalized description can be longer than a btree index
entry allows. Migration 0003 computes the same bytes in SQL for existing rows: change the format
only together with a migration that recomputes every stored fingerprint.
"""

import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from transaction_analytics.domain.csv_parser import ParsedTransaction

# Unit separator. It cannot occur in a normalized description: clean_description collapses it as
# whitespace (and rejects any other control character), and the other fields have fixed formats.
# So distinct keys always serialize to distinct strings.
_SEPARATOR = "\x1f"


@dataclass(frozen=True, slots=True)
class DedupRow:
    transaction: ParsedTransaction
    fingerprint: bytes
    occurrence: int


def fingerprint(transaction: ParsedTransaction) -> bytes:
    """SHA-256 of the dedup key: ISO date, amount in minor units, currency, normalized text."""
    key = _SEPARATOR.join(
        (
            transaction.booked_on.isoformat(),
            str(transaction.amount_minor),
            transaction.currency.value,
            transaction.normalized_description,
        )
    )
    return hashlib.sha256(key.encode("utf-8")).digest()


def assign_occurrences(transactions: Sequence[ParsedTransaction]) -> list[DedupRow]:
    """Number rows with the same key 1, 2, 3, ... in file order."""
    seen: Counter[bytes] = Counter()
    rows = []
    for transaction in transactions:
        key = fingerprint(transaction)
        seen[key] += 1
        rows.append(DedupRow(transaction, key, seen[key]))
    return rows
