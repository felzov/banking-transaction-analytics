"""Request and response bodies. Built from ORM rows explicitly, so no column leaks by accident."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from transaction_analytics.data.models import Account, ImportJob, ImportRowError, User
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus

# Bounds the request before normalization; the real rule (100 characters) is in the domain.
MAX_ACCOUNT_NAME_INPUT_LENGTH = 1000


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=MAX_ACCOUNT_NAME_INPUT_LENGTH)


class AccountOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    @classmethod
    def of(cls, account: Account) -> Self:
        return cls(id=account.id, name=account.name, created_at=account.created_at)


class UserOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> Self:
        return cls(id=user.id, name=user.name, created_at=user.created_at)


class ImportFailureOut(BaseModel):
    code: ImportFailureCode
    message: str


class ImportOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    status: ImportStatus
    total_rows: int
    valid_rows: int
    invalid_rows: int
    # Valid rows the account already held, skipped (ADR 0006); the rest were stored.
    duplicate_rows: int
    imported_rows: int
    error: ImportFailureOut | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, job: ImportJob) -> Self:
        error = None
        if job.error_code is not None and job.error_message is not None:
            error = ImportFailureOut(code=job.error_code, message=job.error_message)
        return cls(
            id=job.id,
            account_id=job.account_id,
            status=job.status,
            total_rows=job.total_rows,
            valid_rows=job.valid_rows,
            invalid_rows=job.invalid_rows,
            duplicate_rows=job.duplicate_rows,
            imported_rows=job.valid_rows - job.duplicate_rows,
            error=error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


class RowErrorOut(BaseModel):
    row_number: int
    code: FieldErrorCode
    # A canonical column name (never a header from the file), or None for whole-row problems.
    column: str | None
    message: str

    @classmethod
    def of(cls, error: ImportRowError) -> Self:
        return cls(
            row_number=error.row_number,
            code=error.code,
            column=error.column_name,
            message=error.message,
        )


class RowErrorPageOut(BaseModel):
    items: list[RowErrorOut]
    next_after_row: int | None
