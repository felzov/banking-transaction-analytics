import dataclasses
from datetime import date

import pytest

from transaction_analytics.domain.csv_parser import ParsedTransaction, parse_statement_csv
from transaction_analytics.domain.dedup import assign_occurrences, fingerprint
from transaction_analytics.domain.fields import clean_description
from transaction_analytics.domain.money import MAX_ABS_AMOUNT_MINOR, Currency

COFFEE = ParsedTransaction(
    row_number=2,
    booked_on=date(2026, 9, 2),
    description="Coffee Shop",
    normalized_description="coffee shop",
    amount_minor=-320,
    currency=Currency.GBP,
)


def test_fingerprint_is_pinned() -> None:
    # Stored fingerprints must stay valid. If this changes, every stored fingerprint needs a
    # migration that recomputes it (ADR 0006), or re-imports stop matching existing rows.
    assert fingerprint(COFFEE).hex() == (
        "119342f052690e79055832b06562abe744856e44f79347805352019ece9b3fa1"
    )
    assert len(fingerprint(COFFEE)) == 32


def test_row_number_and_display_description_are_not_part_of_the_key() -> None:
    other = dataclasses.replace(COFFEE, row_number=40, description="COFFEE SHOP")

    assert fingerprint(other) == fingerprint(COFFEE)


@pytest.mark.parametrize(
    "change",
    [
        {"booked_on": date(2026, 9, 3)},
        {"amount_minor": -321},
        {"amount_minor": 320},  # same digits, opposite direction
        {"currency": Currency.EUR},
        {"normalized_description": "coffee shop 2"},
    ],
    ids=["date", "amount", "sign", "currency", "description"],
)
def test_every_key_component_changes_the_fingerprint(change: dict[str, object]) -> None:
    assert fingerprint(dataclasses.replace(COFFEE, **change)) != fingerprint(COFFEE)  # type: ignore[arg-type]


def test_spelling_variants_of_one_description_share_a_fingerprint() -> None:
    # Whitespace, case and NFKC compatibility forms (full-width letters) normalize to one key.
    content = (
        "date,description,amount,currency\n"
        "2026-09-02,Coffee Shop,-3.20,GBP\n"
        "2026-09-02,  COFFEE   shop ,-3.2,gbp\n"
        "2026-09-02,\uff23\uff4f\uff46\uff46\uff45\uff45 Shop,-3.20,GBP\n"
    ).encode()

    rows = parse_statement_csv(content).transactions

    assert len(rows) == 3
    assert {fingerprint(row) for row in rows} == {fingerprint(COFFEE)}


@pytest.mark.parametrize(
    ("amount_minor", "currency"),
    [
        (-MAX_ABS_AMOUNT_MINOR, Currency.GBP),
        (MAX_ABS_AMOUNT_MINOR, Currency.GBP),
        (5, Currency.JPY),
    ],
)
def test_amount_edges(amount_minor: int, currency: Currency) -> None:
    row = dataclasses.replace(COFFEE, amount_minor=amount_minor, currency=currency)

    assert len(fingerprint(row)) == 32
    assert fingerprint(row) != fingerprint(dataclasses.replace(row, amount_minor=-amount_minor))


def test_separator_cannot_appear_in_a_description() -> None:
    # The key joins its fields with \x1f; distinct keys stay distinct only if no field contains it.
    assert "\x1f" not in clean_description("coffee\x1fshop")


def test_identical_rows_are_numbered_in_file_order() -> None:
    tea = dataclasses.replace(COFFEE, normalized_description="tea")
    file_rows = [
        dataclasses.replace(COFFEE, row_number=2),
        dataclasses.replace(tea, row_number=3),
        dataclasses.replace(COFFEE, row_number=4),
        dataclasses.replace(COFFEE, row_number=5),
        dataclasses.replace(tea, row_number=6),
    ]

    rows = assign_occurrences(file_rows)

    assert [row.transaction for row in rows] == file_rows
    assert [row.occurrence for row in rows] == [1, 1, 2, 3, 2]
    assert [row.fingerprint for row in rows] == [fingerprint(t) for t in file_rows]


def test_distinct_rows_are_all_first_occurrences() -> None:
    rows = assign_occurrences(
        [dataclasses.replace(COFFEE, amount_minor=-100 * n, row_number=n + 2) for n in range(5)]
    )

    assert [row.occurrence for row in rows] == [1] * 5


def test_no_rows() -> None:
    assert assign_occurrences([]) == []
