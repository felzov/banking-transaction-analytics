"""Request and response bodies. Built from ORM rows explicitly, so no column leaks by accident."""

import uuid
from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from transaction_analytics.data.models import (
    Account,
    ImportJob,
    ImportRowError,
    LedgerSync,
    Transaction,
    User,
)
from transaction_analytics.domain.analytics import CurrencyTotals, MonthTotals
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus
from transaction_analytics.domain.ledger import AccountSource, LedgerLink
from transaction_analytics.domain.money import Currency, format_minor

# Bounds the request before normalization; the real rule (100 characters) is in the domain.
MAX_ACCOUNT_NAME_INPUT_LENGTH = 1000
# Bounds the ledger token before its shape is checked (domain/ledger.py: 46 characters).
MAX_LEDGER_TOKEN_INPUT_LENGTH = 200


class LedgerLinkIn(BaseModel):
    """The banking-ledger-api user and account (both ids from the ledger) to read from."""

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    account_id: uuid.UUID

    def to_domain(self) -> LedgerLink:
        return LedgerLink(user_id=self.user_id, account_id=self.account_id)


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=MAX_ACCOUNT_NAME_INPUT_LENGTH)
    # Given: a ledger account, fed only by ledger syncs. Left out: a CSV account (ADR 0009).
    ledger: LedgerLinkIn | None = None


class LedgerLinkOut(BaseModel):
    user_id: uuid.UUID
    account_id: uuid.UUID


class AccountOut(BaseModel):
    id: uuid.UUID
    name: str
    source: AccountSource
    # Present exactly for a ledger account.
    ledger: LedgerLinkOut | None
    created_at: datetime

    @classmethod
    def of(cls, account: Account) -> Self:
        ledger = None
        if account.ledger_user_id is not None and account.ledger_account_id is not None:
            ledger = LedgerLinkOut(
                user_id=account.ledger_user_id, account_id=account.ledger_account_id
            )
        return cls(
            id=account.id,
            name=account.name,
            source=account.source,
            ledger=ledger,
            created_at=account.created_at,
        )


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


# Money (ADR 0008): every amount is two fields from one int, `<name>_minor` (signed minor units,
# what clients compute with) and `<name>` (the same value as an exact decimal string, safe for
# JSON parsers that lose integer precision above 2**53). Negative = money out (ADR 0001).


class TransactionOut(BaseModel):
    id: uuid.UUID
    source: AccountSource
    # The import that stored a CSV row; None for a ledger row.
    import_id: uuid.UUID | None
    # The ledger's entry id of a ledger row, for reconciling with its statement; None for CSV.
    ledger_entry_id: uuid.UUID | None
    booked_on: date
    description: str
    currency: Currency
    amount_minor: int
    amount: str

    @classmethod
    def of(cls, transaction: Transaction) -> Self:
        return cls(
            id=transaction.id,
            source=transaction.source,
            import_id=transaction.import_job_id,
            ledger_entry_id=transaction.ledger_entry_id,
            booked_on=transaction.booked_on,
            description=transaction.description,
            currency=transaction.currency,
            amount_minor=transaction.amount_minor,
            amount=format_minor(transaction.amount_minor, transaction.currency),
        )


class LedgerSyncCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A banking-ledger-api access token for the linked ledger user (log in to the ledger first).
    # Used for this sync only; never stored, logged or returned. SecretStr masks it in reprs.
    ledger_token: SecretStr = Field(max_length=MAX_LEDGER_TOKEN_INPUT_LENGTH)


class LedgerSyncOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    # Entries read from the ledger; duplicate_entries of them were already stored, the
    # imported_entries rest were added.
    fetched_entries: int
    duplicate_entries: int
    imported_entries: int
    created_at: datetime

    @classmethod
    def of(cls, sync: LedgerSync) -> Self:
        return cls(
            id=sync.id,
            account_id=sync.account_id,
            fetched_entries=sync.fetched_entries,
            duplicate_entries=sync.duplicate_entries,
            imported_entries=sync.fetched_entries - sync.duplicate_entries,
            created_at=sync.created_at,
        )


class TransactionPageOut(BaseModel):
    items: list[TransactionOut]
    next_cursor: str | None


class _TotalsFields(BaseModel):
    """The fields of one currency's totals; CurrencyTotalsOut and MonthTotalsOut add their own
    constructor (and, for months, the month)."""

    currency: Currency
    transactions: int
    inflow_minor: int
    inflow: str
    outflow_minor: int
    outflow: str
    net_minor: int
    net: str


def _totals_fields(totals: CurrencyTotals) -> dict[str, object]:
    currency = totals.currency
    return {
        "currency": currency,
        "transactions": totals.transactions,
        "inflow_minor": totals.inflow_minor,
        "inflow": format_minor(totals.inflow_minor, currency),
        "outflow_minor": totals.outflow_minor,
        "outflow": format_minor(totals.outflow_minor, currency),
        "net_minor": totals.net_minor,
        "net": format_minor(totals.net_minor, currency),
    }


class CurrencyTotalsOut(_TotalsFields):
    @classmethod
    def of(cls, totals: CurrencyTotals) -> Self:
        return cls(**_totals_fields(totals))


class MonthTotalsOut(_TotalsFields):
    # "YYYY-MM"; isoformat always pads the year to four digits.
    month: str

    @classmethod
    def of(cls, month: MonthTotals) -> Self:
        return cls(month=month.month.isoformat()[:7], **_totals_fields(month.totals))


class _RangeOut(BaseModel):
    account_id: uuid.UUID
    # The requested range, echoed as given (None = open); both ends inclusive.
    from_: date | None = Field(serialization_alias="from")
    to: date | None


class SummaryOut(_RangeOut):
    totals: list[CurrencyTotalsOut]


class MonthlySummaryOut(_RangeOut):
    months: list[MonthTotalsOut]
