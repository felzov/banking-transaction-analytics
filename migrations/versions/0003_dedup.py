"""Deduplication: fingerprint and occurrence on transactions, duplicate count on import jobs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18

- transactions.fingerprint: SHA-256 of "date \\x1f amount_minor \\x1f currency \\x1f
  normalized_description" (UTF-8), the same bytes as domain/dedup.py computes (ADR 0006).
- transactions.occurrence: the k-th transaction with that fingerprint in its account.
- uq_transactions_dedup (owner_id, account_id, fingerprint, occurrence): imports insert with
  ON CONFLICT DO NOTHING on it, so a row the account already holds is skipped.
- import_jobs.duplicate_rows: how many valid rows were skipped as already present.

Existing rows are backfilled in SQL. Their occurrences are numbered in insertion order, so rows
that earlier re-imports duplicated stay, as occurrences 2, 3, ... of their key. The SQL must
produce exactly what domain/dedup.py produces; tests/integration/test_migrations.py compares them.
Literals only, never domain imports (ADR 0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# chr(31) is the unit separator; to_char gives the ISO form date.isoformat() gives, independent of
# the DateStyle setting; amount_minor::text matches Python's str(int).
BACKFILL_FINGERPRINTS = """
UPDATE transactions
SET fingerprint = sha256(convert_to(
    to_char(booked_on, 'YYYY-MM-DD') || chr(31) || amount_minor::text || chr(31)
    || currency || chr(31) || normalized_description,
    'UTF8'
))
"""

BACKFILL_OCCURRENCES = """
UPDATE transactions AS t
SET occurrence = numbered.occurrence
FROM (
    SELECT id, row_number() OVER (
        PARTITION BY owner_id, account_id, fingerprint
        ORDER BY created_at, import_job_id, row_number
    ) AS occurrence
    FROM transactions
) AS numbered
WHERE t.id = numbered.id
"""


def upgrade() -> None:
    op.add_column(
        "import_jobs",
        sa.Column("duplicate_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_import_jobs_duplicate_rows_valid"),
        "import_jobs",
        "duplicate_rows >= 0 AND duplicate_rows <= valid_rows",
    )

    op.add_column("transactions", sa.Column("fingerprint", sa.LargeBinary(), nullable=True))
    op.add_column("transactions", sa.Column("occurrence", sa.Integer(), nullable=True))
    op.execute(BACKFILL_FINGERPRINTS)
    op.execute(BACKFILL_OCCURRENCES)
    op.alter_column("transactions", "fingerprint", nullable=False)
    op.alter_column("transactions", "occurrence", nullable=False)
    op.create_check_constraint(
        op.f("ck_transactions_fingerprint_length"), "transactions", "octet_length(fingerprint) = 32"
    )
    op.create_check_constraint(
        op.f("ck_transactions_occurrence_positive"), "transactions", "occurrence >= 1"
    )
    op.create_unique_constraint(
        op.f("uq_transactions_dedup"),
        "transactions",
        ["owner_id", "account_id", "fingerprint", "occurrence"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_transactions_dedup"), "transactions", type_="unique")
    op.drop_constraint(op.f("ck_transactions_occurrence_positive"), "transactions", type_="check")
    op.drop_constraint(op.f("ck_transactions_fingerprint_length"), "transactions", type_="check")
    op.drop_column("transactions", "occurrence")
    op.drop_column("transactions", "fingerprint")

    op.drop_constraint(op.f("ck_import_jobs_duplicate_rows_valid"), "import_jobs", type_="check")
    op.drop_column("import_jobs", "duplicate_rows")
