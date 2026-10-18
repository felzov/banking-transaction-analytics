"""Schema guards that need no database. Constraint behaviour is tested in tests/integration/."""

import pytest
from sqlalchemy import Table, UniqueConstraint, Uuid

from transaction_analytics.data.models import ACTIVE_FILE_INDEX, DEDUP_CONSTRAINT, Base

TABLES = sorted(Base.metadata.tables.values(), key=lambda table: table.name)
POSTGRES_MAX_IDENTIFIER_LENGTH = 63


def test_expected_tables() -> None:
    assert [table.name for table in TABLES] == [
        "accounts",
        "import_jobs",
        "import_row_errors",
        "transactions",
    ]


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
def test_every_table_has_non_null_uuid_owner(table: Table) -> None:
    owner_id = table.c.owner_id
    assert isinstance(owner_id.type, Uuid)
    assert owner_id.nullable is False


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
def test_every_foreign_key_includes_owner_id(table: Table) -> None:
    for fk in table.foreign_key_constraints:
        assert "owner_id" in fk.column_keys, fk.name


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
