"""Ledger source: accounts linked to banking-ledger-api, ledger syncs, ledger transactions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22

- accounts.source ('csv' | 'ledger', existing accounts are 'csv') with ledger_user_id and
  ledger_account_id set exactly for ledger accounts; one owner links a ledger account once.
- ledger_syncs: one row per read of a ledger statement, like import_jobs for files.
- transactions.source, with either import_job_id and row_number (csv) or ledger_sync_id and
  ledger_entry_id (ledger). fk_transactions_account becomes fk_transactions_account_source
  (owner_id, account_id, source) -> accounts (owner_id, id, source): every row's source must be
  its account's. It is the one foreign key checked for every row, because a key containing a
  NULL column (import_job_id of a ledger row, ledger_sync_id of a CSV row) is not checked.
- A ledger row has occurrence 1 and the fingerprint sha256('ledger' \\x1f entry id), the same bytes
  as domain/ledger.py; uq_transactions_dedup then holds each ledger entry once per account.

Downgrade refuses while ledger accounts exist: their transactions have no import job, and
financial rows are never deleted by a migration. Literals only, never domain imports (ADR 0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Exactly one set of provenance columns, chosen by the source.
SOURCE_COLUMNS = """
(source = 'csv' AND import_job_id IS NOT NULL AND row_number IS NOT NULL
    AND ledger_sync_id IS NULL AND ledger_entry_id IS NULL)
OR (source = 'ledger' AND import_job_id IS NULL AND row_number IS NULL
    AND ledger_sync_id IS NOT NULL AND ledger_entry_id IS NOT NULL)
"""

# uuid::text is the canonical lowercase form, like Python's str(uuid); chr(31) the unit separator.
LEDGER_FINGERPRINT = """
source <> 'ledger' OR (
    occurrence = 1
    AND fingerprint = sha256(convert_to('ledger' || chr(31) || ledger_entry_id::text, 'UTF8'))
)
"""

REFUSE_DOWNGRADE_WITH_LEDGER_DATA = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM accounts WHERE source = 'ledger') THEN
        RAISE EXCEPTION 'ledger accounts exist; migration 0006 cannot be downgraded';
    END IF;
END
$$
"""


def _one_of(column: str, values: Sequence[str]) -> sa.ColumnElement[bool]:
    return sa.column(column, sa.Text()).in_(values)


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("source", sa.Text(), server_default=sa.text("'csv'"), nullable=False),
    )
    op.add_column("accounts", sa.Column("ledger_user_id", sa.Uuid(), nullable=True))
    op.add_column("accounts", sa.Column("ledger_account_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        op.f("ck_accounts_source_allowed"), "accounts", _one_of("source", ("csv", "ledger"))
    )
    op.create_check_constraint(
        op.f("ck_accounts_ledger_link_iff_ledger"),
        "accounts",
        "(source = 'ledger') = (ledger_user_id IS NOT NULL)"
        " AND (source = 'ledger') = (ledger_account_id IS NOT NULL)",
    )
    op.create_unique_constraint(
        op.f("uq_accounts_owner_id_id_source"), "accounts", ["owner_id", "id", "source"]
    )
    op.create_unique_constraint(
        op.f("uq_accounts_owner_id_ledger_account_id"),
        "accounts",
        ["owner_id", "ledger_account_id"],
    )

    op.create_table(
        "ledger_syncs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("fetched_entries", sa.Integer(), nullable=False),
        sa.Column("duplicate_entries", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_syncs")),
        sa.ForeignKeyConstraint(
            ["owner_id", "account_id"],
            ["accounts.owner_id", "accounts.id"],
            name=op.f("fk_ledger_syncs_account"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "owner_id", "account_id", "id", name=op.f("uq_ledger_syncs_owner_id_account_id_id")
        ),
        # fetched_entries >= 0 follows.
        sa.CheckConstraint(
            "duplicate_entries >= 0 AND duplicate_entries <= fetched_entries",
            name=op.f("ck_ledger_syncs_duplicate_entries_valid"),
        ),
    )

    op.add_column(
        "transactions",
        sa.Column("source", sa.Text(), server_default=sa.text("'csv'"), nullable=False),
    )
    op.add_column("transactions", sa.Column("ledger_sync_id", sa.Uuid(), nullable=True))
    op.add_column("transactions", sa.Column("ledger_entry_id", sa.Uuid(), nullable=True))
    op.alter_column("transactions", "import_job_id", nullable=True)
    op.alter_column("transactions", "row_number", nullable=True)
    op.create_check_constraint(
        op.f("ck_transactions_source_allowed"),
        "transactions",
        _one_of("source", ("csv", "ledger")),
    )
    op.create_check_constraint(
        op.f("ck_transactions_source_columns"), "transactions", SOURCE_COLUMNS
    )
    op.create_check_constraint(
        op.f("ck_transactions_ledger_fingerprint"), "transactions", LEDGER_FINGERPRINT
    )
    op.create_foreign_key(
        op.f("fk_transactions_ledger_sync"),
        "transactions",
        "ledger_syncs",
        ["owner_id", "account_id", "ledger_sync_id"],
        ["owner_id", "account_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_transactions_account_source"),
        "transactions",
        "accounts",
        ["owner_id", "account_id", "source"],
        ["owner_id", "id", "source"],
        ondelete="RESTRICT",
    )
    # Covered by fk_transactions_account_source (same columns plus source, all NOT NULL).
    op.drop_constraint(op.f("fk_transactions_account"), "transactions", type_="foreignkey")


def downgrade() -> None:
    op.execute(REFUSE_DOWNGRADE_WITH_LEDGER_DATA)

    op.create_foreign_key(
        op.f("fk_transactions_account"),
        "transactions",
        "accounts",
        ["owner_id", "account_id"],
        ["owner_id", "id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(op.f("fk_transactions_account_source"), "transactions", type_="foreignkey")
    op.drop_constraint(op.f("fk_transactions_ledger_sync"), "transactions", type_="foreignkey")
    op.drop_constraint(op.f("ck_transactions_ledger_fingerprint"), "transactions", type_="check")
    op.drop_constraint(op.f("ck_transactions_source_columns"), "transactions", type_="check")
    op.drop_constraint(op.f("ck_transactions_source_allowed"), "transactions", type_="check")
    op.alter_column("transactions", "row_number", nullable=False)
    op.alter_column("transactions", "import_job_id", nullable=False)
    op.drop_column("transactions", "ledger_entry_id")
    op.drop_column("transactions", "ledger_sync_id")
    op.drop_column("transactions", "source")

    op.drop_table("ledger_syncs")

    op.drop_constraint(op.f("uq_accounts_owner_id_ledger_account_id"), "accounts", type_="unique")
    op.drop_constraint(op.f("uq_accounts_owner_id_id_source"), "accounts", type_="unique")
    op.drop_constraint(op.f("ck_accounts_ledger_link_iff_ledger"), "accounts", type_="check")
    op.drop_constraint(op.f("ck_accounts_source_allowed"), "accounts", type_="check")
    op.drop_column("accounts", "ledger_account_id")
    op.drop_column("accounts", "ledger_user_id")
    op.drop_column("accounts", "source")
