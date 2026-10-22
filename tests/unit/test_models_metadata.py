"""Schema guards that need no database. Constraint behaviour is tested in tests/integration/."""

import pytest
from sqlalchemy import Table, UniqueConstraint, Uuid

from transaction_analytics.data.models import (
    ACTIVE_FILE_INDEX,
    DEDUP_CONSTRAINT,
    LEDGER_LINK_CONSTRAINT,
    Base,
)

TABLES = sorted(Base.metadata.tables.values(), key=lambda table: table.name)
# users is what owner_id refers to (ADR 0007); every other table belongs to an owner.
OWNED_TABLES = [table for table in TABLES if table.name != "users"]
POSTGRES_MAX_IDENTIFIER_LENGTH = 63


def test_expected_tables() -> None:
    assert [table.name for table in TABLES] == [
        "accounts",
        "api_tokens",
        "import_jobs",
        "import_row_errors",
        "ledger_syncs",
        "transactions",
        "users",
    ]


@pytest.mark.parametrize("table", OWNED_TABLES, ids=lambda table: table.name)
def test_every_table_has_non_null_uuid_owner(table: Table) -> None:
    owner_id = table.c.owner_id
    assert isinstance(owner_id.type, Uuid)
    assert owner_id.nullable is False


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
def test_every_foreign_key_includes_owner_id(table: Table) -> None:
    for fk in table.foreign_key_constraints:
        assert "owner_id" in fk.column_keys, fk.name


def test_users_is_the_only_table_without_an_owner() -> None:
    assert [table.name for table in TABLES if "owner_id" not in table.c] == ["users"]


@pytest.mark.parametrize("table", ["accounts", "api_tokens"])
def test_owner_id_references_a_user(table: str) -> None:
    # accounts carries it to the other tables through their composite keys to accounts.
    (fk,) = [
        fk
        for fk in Base.metadata.tables[table].foreign_key_constraints
        if fk.referred_table.name == "users"
    ]
    assert fk.column_keys == ["owner_id"]
    assert [element.column.name for element in fk.elements] == ["id"]
    assert fk.ondelete == "RESTRICT"


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
def test_constraints_and_indexes_have_explicit_short_names(table: Table) -> None:
    for item in [*table.constraints, *table.indexes]:
        assert item.name, f"unnamed constraint or index on {table.name}"
        assert len(str(item.name)) <= POSTGRES_MAX_IDENTIFIER_LENGTH, item.name


def test_active_file_index_is_unique_and_partial() -> None:
    (index,) = Base.metadata.tables["import_jobs"].indexes

    assert index.name == ACTIVE_FILE_INDEX
    assert index.unique
    assert [column.name for column in index.columns] == ["owner_id", "account_id", "file_sha256"]
    assert str(index.dialect_options["postgresql"]["where"]) == "status <> 'failed'"


def test_tokens_are_indexed_by_owner_in_listing_order() -> None:
    # Review fix: list_tokens and the RESTRICT check of fk_api_tokens_owner scanned the table.
    (index,) = Base.metadata.tables["api_tokens"].indexes

    assert index.name == "ix_api_tokens_owner_id_created_at"
    assert [column.name for column in index.columns] == ["owner_id", "created_at"]
    assert not index.unique


def test_transactions_are_indexed_by_account_in_listing_order() -> None:
    # Listing pages by (booked_on, id) within one account; analytics filter on booked_on.
    (index,) = Base.metadata.tables["transactions"].indexes

    assert index.name == "ix_transactions_owner_id_account_id_booked_on_id"
    assert [column.name for column in index.columns] == [
        "owner_id",
        "account_id",
        "booked_on",
        "id",
    ]
    assert not index.unique


def test_dedup_constraint_is_owner_scoped_per_occurrence() -> None:
    table = Base.metadata.tables["transactions"]
    (constraint,) = [c for c in table.constraints if c.name == DEDUP_CONSTRAINT]

    assert isinstance(constraint, UniqueConstraint)
    assert [column.name for column in constraint.columns] == [
        "owner_id",
        "account_id",
        "fingerprint",
        "occurrence",
    ]


def _foreign_key(table: str, name: str) -> tuple[list[str], str, list[str]]:
    (fk,) = [fk for fk in Base.metadata.tables[table].foreign_key_constraints if fk.name == name]
    return fk.column_keys, fk.referred_table.name, [element.column.name for element in fk.elements]


def test_every_transaction_must_match_its_accounts_source() -> None:
    # ADR 0009: the one foreign key checked for every row (the job and sync keys contain a NULL
    # column for rows of the other source, and such keys are not checked).
    assert _foreign_key("transactions", "fk_transactions_account_source") == (
        ["owner_id", "account_id", "source"],
        "accounts",
        ["owner_id", "id", "source"],
    )
    names = {fk.name for fk in Base.metadata.tables["transactions"].foreign_key_constraints}
    assert "fk_transactions_account" not in names  # covered by the key above


def test_ledger_rows_belong_to_a_sync_of_the_same_account() -> None:
    assert _foreign_key("transactions", "fk_transactions_ledger_sync") == (
        ["owner_id", "account_id", "ledger_sync_id"],
        "ledger_syncs",
        ["owner_id", "account_id", "id"],
    )


def test_a_ledger_account_is_linked_once_per_owner() -> None:
    table = Base.metadata.tables["accounts"]
    (constraint,) = [c for c in table.constraints if c.name == LEDGER_LINK_CONSTRAINT]

    assert isinstance(constraint, UniqueConstraint)
    assert [column.name for column in constraint.columns] == ["owner_id", "ledger_account_id"]


def test_ledger_sync_keys_are_the_ones_referenced() -> None:
    # Review fix: a UNIQUE (owner_id, id) "for the foreign key from transactions" was maintained on
    # every insert, but nothing referenced it. The only key transactions reference is this one.
    table = Base.metadata.tables["ledger_syncs"]
    uniques = [
        [column.name for column in constraint.columns]
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert uniques == [_foreign_key("transactions", "fk_transactions_ledger_sync")[2]]
