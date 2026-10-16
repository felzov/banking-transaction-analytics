"""SQL built by migration helpers, compiled without a database."""

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy.dialects import postgresql

MIGRATION = Path(__file__).parents[2] / "migrations" / "versions" / "0001_initial_schema.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0001", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_allowed_values_are_rendered_as_escaped_literals() -> None:
    one_of = _load_migration()._one_of

    sql = str(
        one_of("code", ["plain", "it's"]).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    # repr() would have produced "it's": a quoted identifier in SQL, not a string.
    assert sql == "code IN ('plain', 'it''s')"
