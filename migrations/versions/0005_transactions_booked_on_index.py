"""Index transactions by account and booking date for listing and analytics.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20

- ix_transactions_owner_id_account_id_booked_on_id: every transaction read in Phase 6 filters by
  owner and account and a booking-date range; the listing pages by (booked_on, id), so the index
  ends in id and a page is read in index order instead of sorting the account (ADR 0008).

Not CREATE INDEX CONCURRENTLY: that cannot run inside the migration's transaction, and the table
is small at this stage. A plain build blocks writes to transactions while it runs (Phase 8).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_transactions_owner_id_account_id_booked_on_id"


def upgrade() -> None:
    op.create_index(op.f(INDEX), "transactions", ["owner_id", "account_id", "booked_on", "id"])


def downgrade() -> None:
    op.drop_index(op.f(INDEX), table_name="transactions")
