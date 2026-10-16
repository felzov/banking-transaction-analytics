"""Initial schema: accounts, import_jobs, transactions, import_row_errors.

Revision ID: 0001
Revises:
Create Date: 2026-10-16

Allowed values in CHECK constraints are written as literals, never imported from the domain:
a migration is history and must not change when the code changes (ADR 0003).
Names are wrapped in op.f() so the naming convention does not prefix them a second time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENCIES = ("GBP", "EUR", "USD", "JPY")
IMPORT_STATUSES = ("pending", "processing", "completed", "failed")
IMPORT_FAILURE_CODES = (
    "empty_file",
    "file_too_large",
    "invalid_encoding",
    "malformed_csv",
    "missing_columns",
    "duplicate_columns",
    "too_many_rows",
    "no_data_rows",
    "internal_error",
)
FIELD_ERROR_CODES = (
    "missing_value",
    "invalid_amount_format",
    "amount_too_precise",
    "amount_out_of_range",
    "invalid_date",
    "unsupported_currency",
    "description_too_long",
    "column_count_mismatch",
    "invalid_characters",
)
CSV_COLUMNS = ("date", "description", "amount", "currency")


def _one_of(column: str, values: Sequence[str]) -> sa.ColumnElement[bool]:
    # An expression, not a formatted string: the DDL compiler renders the values as properly
    # quoted and escaped SQL literals.
    return sa.column(column, sa.Text()).in_(values)


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "accounts",
        _id(),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_accounts_owner_id_id")),
        sa.CheckConstraint(
            "btrim(name) <> '' AND char_length(name) <= 100", name=op.f("ck_accounts_name_valid")
        ),
    )

    op.create_table(
        "import_jobs",
        _id(),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("total_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("valid_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("invalid_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_jobs")),
        sa.ForeignKeyConstraint(
            ["owner_id", "account_id"],
            ["accounts.owner_id", "accounts.id"],
            name=op.f("fk_import_jobs_account"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "account_id",
            "file_sha256",
            name=op.f("uq_import_jobs_owner_id_account_id_file_sha256"),
        ),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_import_jobs_owner_id_id")),
        sa.UniqueConstraint(
            "owner_id", "account_id", "id", name=op.f("uq_import_jobs_owner_id_account_id_id")
        ),
        sa.CheckConstraint(
            _one_of("status", IMPORT_STATUSES), name=op.f("ck_import_jobs_status_allowed")
        ),
        # Lowercase only: otherwise the same file hashed as upper- and lowercase hex would
        # pass the uniqueness constraint twice.
        sa.CheckConstraint(
            "file_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_import_jobs_file_sha256_hex")
        ),
        # total_rows >= 0 follows from these two.
        sa.CheckConstraint(
            "valid_rows >= 0 AND invalid_rows >= 0",
            name=op.f("ck_import_jobs_row_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "valid_rows + invalid_rows = total_rows", name=op.f("ck_import_jobs_row_counts_add_up")
        ),
        sa.CheckConstraint(
            _one_of("error_code", IMPORT_FAILURE_CODES),
            name=op.f("ck_import_jobs_error_code_allowed"),
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name=op.f("ck_import_jobs_error_code_iff_failed"),
        ),
        sa.CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)",
            name=op.f("ck_import_jobs_error_message_iff_code"),
        ),
        sa.CheckConstraint(
            "char_length(error_message) BETWEEN 1 AND 500",
            name=op.f("ck_import_jobs_error_message_length"),
        ),
        sa.CheckConstraint(
            "(started_at IS NULL) = (status = 'pending')",
            name=op.f("ck_import_jobs_started_at_iff_not_pending"),
        ),
        sa.CheckConstraint(
            "(finished_at IS NOT NULL) = (status IN ('completed', 'failed'))",
            name=op.f("ck_import_jobs_finished_at_iff_finished"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_import_jobs_finished_after_started"),
        ),
    )

    op.create_table(
        "transactions",
        _id(),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("import_job_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("booked_on", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("normalized_description", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.ForeignKeyConstraint(
            ["owner_id", "account_id", "import_job_id"],
            ["import_jobs.owner_id", "import_jobs.account_id", "import_jobs.id"],
            name=op.f("fk_transactions_import_job"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "account_id"],
            ["accounts.owner_id", "accounts.id"],
            name=op.f("fk_transactions_account"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "import_job_id", "row_number", name=op.f("uq_transactions_import_job_id_row_number")
        ),
        # Row 1 is the CSV header (ADR 0002).
        sa.CheckConstraint("row_number >= 2", name=op.f("ck_transactions_row_number_after_header")),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 500",
            name=op.f("ck_transactions_description_length"),
        ),
        # casefold() can triple the length ("ΐ" -> 3 code points), hence 3 x 500.
        sa.CheckConstraint(
            "char_length(normalized_description) BETWEEN 1 AND 1500",
            name=op.f("ck_transactions_normalized_description_length"),
        ),
        sa.CheckConstraint(
            _one_of("currency", CURRENCIES), name=op.f("ck_transactions_currency_allowed")
        ),
    )

    op.create_table(
        "import_row_errors",
        _id(),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("import_job_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("column_name", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_row_errors")),
        sa.ForeignKeyConstraint(
            ["owner_id", "import_job_id"],
            ["import_jobs.owner_id", "import_jobs.id"],
            name=op.f("fk_import_row_errors_import_job"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "import_job_id",
            "row_number",
            name=op.f("uq_import_row_errors_import_job_id_row_number"),
        ),
        sa.CheckConstraint(
            "row_number >= 2", name=op.f("ck_import_row_errors_row_number_after_header")
        ),
        sa.CheckConstraint(
            _one_of("code", FIELD_ERROR_CODES), name=op.f("ck_import_row_errors_code_allowed")
        ),
        sa.CheckConstraint(
            _one_of("column_name", CSV_COLUMNS),
            name=op.f("ck_import_row_errors_column_name_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(message) BETWEEN 1 AND 500",
            name=op.f("ck_import_row_errors_message_length"),
        ),
    )


def downgrade() -> None:
    op.drop_table("import_row_errors")
    op.drop_table("transactions")
    op.drop_table("import_jobs")
    op.drop_table("accounts")
