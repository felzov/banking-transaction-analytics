"""Valid-by-default rows for integration tests. Override one field to break one rule.

All values are synthetic. IDs are set client-side so related rows can be built before a flush.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from transaction_analytics.data.models import Account, ImportJob, ImportRowError, Transaction
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus
from transaction_analytics.domain.money import Currency

# Fixed, fake owners. Real ownership arrives with auth in Phase 5.
OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")

STARTED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 10, 1, 12, 1, tzinfo=UTC)


def sha256_hex(seed: int = 0) -> str:
    return f"{seed:064x}"


def account(owner_id: uuid.UUID = OWNER_ID, **overrides: Any) -> Account:
    values: dict[str, Any] = {"id": uuid.uuid4(), "owner_id": owner_id, "name": "Current account"}
    return Account(**(values | overrides))


def job(acc: Account, **overrides: Any) -> ImportJob:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "owner_id": acc.owner_id,
        "account_id": acc.id,
        "status": ImportStatus.PENDING,
        "file_sha256": sha256_hex(),
        "total_rows": 0,
        "valid_rows": 0,
        "invalid_rows": 0,
    }
    return ImportJob(**(values | overrides))


def completed(acc: Account, **overrides: Any) -> ImportJob:
    values: dict[str, Any] = {
        "status": ImportStatus.COMPLETED,
        "started_at": STARTED,
        "finished_at": FINISHED,
        "total_rows": 3,
        "valid_rows": 2,
        "invalid_rows": 1,
    }
    return job(acc, **(values | overrides))


def failed(acc: Account, **overrides: Any) -> ImportJob:
    values: dict[str, Any] = {
        "status": ImportStatus.FAILED,
        "started_at": STARTED,
        "finished_at": FINISHED,
        "error_code": ImportFailureCode.MALFORMED_CSV,
        "error_message": "File is not valid CSV (check quoting).",
    }
    return job(acc, **(values | overrides))


def transaction(import_job: ImportJob, **overrides: Any) -> Transaction:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "owner_id": import_job.owner_id,
        "account_id": import_job.account_id,
        "import_job_id": import_job.id,
        "row_number": 2,
        "booked_on": date(2026, 9, 1),
        "description": "Coffee Shop",
        "normalized_description": "coffee shop",
        "amount_minor": -320,
        "currency": Currency.GBP,
    }
    return Transaction(**(values | overrides))


def row_error(import_job: ImportJob, **overrides: Any) -> ImportRowError:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "owner_id": import_job.owner_id,
        "import_job_id": import_job.id,
        "row_number": 3,
        "code": FieldErrorCode.INVALID_DATE,
        "column_name": "date",
        "message": "Date is not a real calendar date.",
    }
    return ImportRowError(**(values | overrides))
