from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from transaction_analytics.data.schema import EXPECTED_SCHEMA_REVISION

ROOT = Path(__file__).resolve().parents[2]


def test_expected_revision_is_the_newest_migration() -> None:
    # /ready compares the database with this constant (ADR 0010). A new migration must update it,
    # or every instance would report "not ready" after `alembic upgrade head`. get_current_head
    # also fails if the migrations ever branch into several heads.
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))

    assert scripts.get_current_head() == EXPECTED_SCHEMA_REVISION
