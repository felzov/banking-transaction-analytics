"""Import API: re-upload after a failed import, stricter account names.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17

- The unique rule on (owner_id, account_id, file_sha256) becomes a partial unique index that
  ignores failed jobs, so a file whose import failed can be uploaded again (ADR 0004).
- ck_accounts_name_valid matches what the domain guarantees and SQL can check without depending
  on the database locale: 1-100 characters, no leading/trailing spaces, no control characters.
  Unicode whitespace such as a no-break space is left to the domain (domain/accounts.py).

Upgrade assumes no existing account name breaks the stricter CHECK (nothing wrote accounts before
Phase 3). If one does, ADD CONSTRAINT fails and the whole migration rolls back: fix those names
first.

Downgrade restores the full unique constraint; it fails if a file has more than one import for
the same account (possible once failed attempts are retried). Literals only, never domain
imports (ADR 0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILE_COLUMNS = ["owner_id", "account_id", "file_sha256"]


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_import_jobs_owner_id_account_id_file_sha256"), "import_jobs", type_="unique"
    )
    op.create_index(
        op.f("uq_import_jobs_active_file_sha256"),
        "import_jobs",
        FILE_COLUMNS,
        unique=True,
        postgresql_where=sa.text("status <> 'failed'"),
    )

    op.drop_constraint(op.f("ck_accounts_name_valid"), "accounts", type_="check")
    op.create_check_constraint(
        op.f("ck_accounts_name_valid"),
        "accounts",
        "char_length(name) BETWEEN 1 AND 100 AND name = btrim(name) AND name !~ '[[:cntrl:]]'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_accounts_name_valid"), "accounts", type_="check")
    op.create_check_constraint(
        op.f("ck_accounts_name_valid"),
        "accounts",
        "btrim(name) <> '' AND char_length(name) <= 100",
    )

    op.drop_index(op.f("uq_import_jobs_active_file_sha256"), table_name="import_jobs")
    op.create_unique_constraint(
        op.f("uq_import_jobs_owner_id_account_id_file_sha256"), "import_jobs", FILE_COLUMNS
    )
