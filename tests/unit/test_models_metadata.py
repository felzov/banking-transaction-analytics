"""Schema guards that need no database. Constraint behaviour is tested in tests/integration/."""

import pytest
from sqlalchemy import Table, Uuid

from transaction_analytics.data.models import Base

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
def test_constraints_have_explicit_short_names(table: Table) -> None:
    for constraint in table.constraints:
        assert constraint.name, f"unnamed constraint on {table.name}"
        assert len(str(constraint.name)) <= POSTGRES_MAX_IDENTIFIER_LENGTH, constraint.name
