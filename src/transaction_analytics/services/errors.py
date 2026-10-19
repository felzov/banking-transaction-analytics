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
