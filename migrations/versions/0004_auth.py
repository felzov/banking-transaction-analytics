"""Authentication: users, API tokens, and a foreign key from accounts to users.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19

- users: one row per owner; owner_id in every other table is a user id (ADR 0007).
- api_tokens: bearer tokens, stored only as the SHA-256 of the token text; indexed by owner for
  listing them and for the RESTRICT check of its foreign key.
- fk_accounts_owner: accounts.owner_id -> users.id. The other tables reach accounts through their
  composite foreign keys (import_jobs -> accounts (owner_id, id), ...), so every owner_id now names
  a user.

Owners that already have accounts (the Phase 3-4 DEV_OWNER_ID placeholder) get a users row before
the foreign key is added, so their data stays usable: issue a token for that id with the CLI.

Downgrade drops the foreign key and both tables; users and tokens are lost, accounts stay.
Literals only, never domain imports (ADR 0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same rule as ck_accounts_name_valid (migration 0002, domain/labels.py).
NAME_VALID = "char_length(name) BETWEEN 1 AND 100 AND name = btrim(name) AND name !~ '[[:cntrl:]]'"

BACKFILL_USERS = """
INSERT INTO users (id, name)
SELECT DISTINCT owner_id, 'Migrated owner' FROM accounts
"""


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.CheckConstraint(NAME_VALID, name=op.f("ck_users_name_valid")),
        sa.CheckConstraint(
            "disabled_at >= created_at", name=op.f("ck_users_disabled_after_created")
        ),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_sha256", sa.LargeBinary(), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_api_tokens_owner"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("token_sha256", name=op.f("uq_api_tokens_token_sha256")),
        sa.CheckConstraint(NAME_VALID, name=op.f("ck_api_tokens_name_valid")),
        sa.CheckConstraint(
            "octet_length(token_sha256) = 32", name=op.f("ck_api_tokens_token_sha256_length")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_api_tokens_expires_after_created")
        ),
        sa.CheckConstraint(
            "revoked_at >= created_at", name=op.f("ck_api_tokens_revoked_after_created")
        ),
    )

    op.create_index(
        op.f("ix_api_tokens_owner_id_created_at"), "api_tokens", ["owner_id", "created_at"]
    )

    op.execute(BACKFILL_USERS)
    op.create_foreign_key(
        op.f("fk_accounts_owner"), "accounts", "users", ["owner_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_accounts_owner"), "accounts", type_="foreignkey")
    op.drop_table("api_tokens")
    op.drop_table("users")
