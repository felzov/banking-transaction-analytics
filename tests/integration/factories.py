"""Valid-by-default rows for integration tests. Override one field to break one rule.

All values are synthetic. IDs are set client-side so related rows can be built before a flush.
"""

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from transaction_analytics.data.models import (
    Account,
    ApiToken,
    ImportJob,
    ImportRowError,
    Transaction,
    User,
)
from transaction_analytics.domain.errors import FieldErrorCode
from transaction_analytics.domain.imports import ImportFailureCode, ImportStatus
from transaction_analytics.domain.money import Currency

# Fixed, fake owners. conftest.py commits a users row for each into every migrated test database,
# so tests can create accounts for them directly; tests of authentication create their own users.
OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
SEEDED_OWNERS = {OWNER_ID: "Test owner", OTHER_OWNER_ID: "Other test owner"}

STARTED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 10, 1, 12, 1, tzinfo=UTC)


def sha256_hex(seed: int = 0) -> str:
    return f"{seed:064x}"


def user(**overrides: Any) -> User:
    values: dict[str, Any] = {"id": uuid.uuid4(), "name": "Test user"}
    return User(**(values | overrides))


def api_token(owner_id: uuid.UUID = OWNER_ID, **overrides: Any) -> ApiToken:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "owner_id": owner_id,
        "name": "laptop",
        "token_sha256": os.urandom(32),
        # created_at is the database's now(), i.e. the test transaction's start: well before this.
        "expires_at": datetime.now(UTC) + timedelta(days=90),
    }
    return ApiToken(**(values | overrides))


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
        # Random, not derived from the fields: rows built here never collide on the dedup key,
        # so a test about another rule cannot be rejected by uq_transactions_dedup first.
        "fingerprint": os.urandom(32),
        "occurrence": 1,
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
