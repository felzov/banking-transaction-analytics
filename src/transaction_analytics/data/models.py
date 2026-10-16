"""SQLAlchemy models (ADR 0003).

Ownership is enforced by the schema, not only by queries: every table has owner_id, and composite
foreign keys include it, so a row cannot reference another owner's account or import job.
Column order in those keys is owner_id first, so the same unique indexes serve owner-scoped lookups.

CHECK constraints live only in the migrations (migrations/versions/). Alembic autogenerate does
not compare CHECKs, so a copy here could drift silently; the integration tests exercise them
against the migrated database instead.

No relationship() yet: nothing needs them, and implicit lazy loads fail in async code.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Dialect,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus
from transaction_analytics.domain.money import Currency

# Deterministic constraint names: migrations can drop them, and tests and the Phase 3 error
# mapping can identify which rule a row broke. FKs are always named explicitly (short names).
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(constraint_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class TextEnum[E: StrEnum](TypeDecorator[E]):
    """Stores a StrEnum as TEXT and reads it back as the enum.

    Binding does not validate: the database CHECK constraint is the single gate, so an invalid
    value fails the same way whether it comes from the ORM or from raw SQL.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum_class: type[E]) -> None:
        super().__init__()
        self.enum_class = enum_class

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> E | None:
        return None if value is None else self.enum_class(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=func.gen_random_uuid())


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("owner_id", "id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "account_id"],
            ["accounts.owner_id", "accounts.id"],
            name="account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("owner_id", "account_id", "file_sha256"),
        # Targets for the composite foreign keys from transactions and import_row_errors.
        UniqueConstraint("owner_id", "id"),
        UniqueConstraint("owner_id", "account_id", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    status: Mapped[ImportStatus] = mapped_column(
        TextEnum(ImportStatus), server_default=ImportStatus.PENDING.value
    )
    file_sha256: Mapped[str] = mapped_column(Text)
    total_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    valid_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    invalid_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_code: Mapped[ImportFailureCode | None] = mapped_column(TextEnum(ImportFailureCode))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Owner and account must match the import job's, not just exist.
        ForeignKeyConstraint(
            ["owner_id", "account_id", "import_job_id"],
            ["import_jobs.owner_id", "import_jobs.account_id", "import_jobs.id"],
            name="import_job",
            ondelete="RESTRICT",
        ),
        # Redundant while import_job_id is NOT NULL; the only guard once transactions can come
        # from another source without an import job (Phase 7).
        ForeignKeyConstraint(
            ["owner_id", "account_id"],
            ["accounts.owner_id", "accounts.id"],
            name="account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("import_job_id", "row_number"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    import_job_id: Mapped[uuid.UUID]
    row_number: Mapped[int] = mapped_column(Integer)
    booked_on: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    normalized_description: Mapped[str] = mapped_column(Text)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[Currency] = mapped_column(TextEnum(Currency))
    created_at: Mapped[datetime] = _created_at()


class ImportRowError(Base):
    __tablename__ = "import_row_errors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "import_job_id"],
            ["import_jobs.owner_id", "import_jobs.id"],
            name="import_job",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("import_job_id", "row_number"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID]
    import_job_id: Mapped[uuid.UUID]
    row_number: Mapped[int] = mapped_column(Integer)
    code: Mapped[FieldErrorCode] = mapped_column(TextEnum(FieldErrorCode))
    # One of the canonical column names (never a header from the file), or NULL for row-level
    # problems such as a wrong number of values.
    column_name: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
