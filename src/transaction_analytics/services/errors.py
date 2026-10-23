"""Errors the services raise. Stable code plus fixed text; no HTTP and no values from input."""

import uuid
from typing import ClassVar

from transaction_analytics.domain.imports import INTERNAL_ERROR_MESSAGE


class ServiceError(Exception):
    code: ClassVar[str]
    message: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.message)


class NotAuthenticatedError(ServiceError):
    """No live API token. One error for every reason (missing, unknown, expired, revoked, user
    disabled), so a response does not tell a caller which tokens exist."""

    code = "not_authenticated"
    message = "A valid API token is required."

    def __init__(self, *, token_sent: bool) -> None:
        super().__init__()
        # Only for the WWW-Authenticate header (RFC 6750): never which check failed.
        self.token_sent = token_sent


class UserNotFoundError(ServiceError):
    code = "user_not_found"
    message = "User not found."


class UserDisabledError(ServiceError):
    code = "user_disabled"
    message = "User is disabled."


class TokenNotFoundError(ServiceError):
    code = "token_not_found"
    message = "API token not found."


class AccountNotFoundError(ServiceError):
    code = "account_not_found"
    message = "Account not found."


class ImportNotFoundError(ServiceError):
    code = "import_not_found"
    message = "Import not found."


class DuplicateImportError(ServiceError):
    code = "duplicate_import"
    message = "This file has already been imported into this account."

    def __init__(self, existing_import_id: uuid.UUID) -> None:
        super().__init__()
        self.existing_import_id = existing_import_id


class ImportProcessingError(ServiceError):
    """Unexpected failure while storing an import. The job is recorded as failed when possible."""

    code = "internal_error"
    message = INTERNAL_ERROR_MESSAGE

    def __init__(self, failed_import_id: uuid.UUID | None) -> None:
        super().__init__()
        self.failed_import_id = failed_import_id


class AccountSourceMismatchError(ServiceError):
    """A CSV upload to a ledger account, or a ledger sync of a CSV account (ADR 0009)."""

    code = "account_source_mismatch"
    message = "This account's transactions come from another source."


class LedgerAccountAlreadyLinkedError(ServiceError):
    code = "ledger_account_already_linked"
    message = "This ledger account is already linked to one of your accounts."


class LedgerSyncNotFoundError(ServiceError):
    code = "ledger_sync_not_found"
    message = "Ledger sync not found."


class LedgerNotConfiguredError(ServiceError):
    code = "ledger_not_configured"
    message = "Ledger syncs are not configured on this server."


class LedgerTokenRejectedError(ServiceError):
    code = "ledger_token_rejected"
    message = "The ledger rejected the token. Log in to the ledger again and retry."


class LedgerAccountUnavailableError(ServiceError):
    code = "ledger_account_unavailable"
    message = "The linked ledger account cannot be read with this token."


class LedgerTooManyEntriesError(ServiceError):
    code = "ledger_too_many_entries"
    message = "The ledger account has more entries to read than one sync may read."


class LedgerUnavailableError(ServiceError):
    code = "ledger_unavailable"
    message = "The ledger is not available. Please try again later."


class NotReadyError(ServiceError):
    """The readiness probe failed (ADR 0010). Why is logged, never returned: the probe needs no
    token."""

    code = "not_ready"
    message = "Service is not ready."


class LedgerInvalidResponseError(ServiceError):
    """The ledger answered, but with something malformed or inconsistent. Nothing is stored."""

    code = "ledger_invalid_response"
    message = "The ledger returned an invalid statement. Nothing was stored."
