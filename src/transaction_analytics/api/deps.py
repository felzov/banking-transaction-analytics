"""Request dependencies. Tests replace these through app.dependency_overrides.

All are `async def`: FastAPI runs a sync dependency in its threadpool, which would cost a thread
hop per request just to return a ready value (and compete with the parser's worker threads).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import User
from transaction_analytics.integrations.ledger import LedgerConfig
from transaction_analytics.services import auth as auth_service
from transaction_analytics.services.imports import ImportLimits
from transaction_analytics.services.ledger_sync import LedgerSettings
from transaction_analytics.settings import Settings, get_settings


async def get_app_settings() -> Settings:
    return get_settings()  # cached after the first call


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # The sessionmaker is created in the app lifespan (main.py). Services commit explicitly;
    # closing the session rolls back anything left uncommitted.
    async with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Parses "Authorization: Bearer <token>" and documents the scheme in OpenAPI. auto_error=False: a
# missing or non-Bearer header gets the same 401, in the error envelope, as an unknown token.
_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="bta_<43 characters>",
    description="An API token issued with the CLI (README, Authentication).",
)


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """The user whose API token the request carries (ADR 0007); 401 otherwise."""
    token = None if credentials is None else credentials.credentials
    return await auth_service.authenticate(session, token=token)


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_owner_id(user: CurrentUserDep) -> uuid.UUID:
    """What routes and services scope by. Tests that are not about authentication override this."""
    return user.id


async def get_import_limits(settings: SettingsDep) -> ImportLimits:
    return ImportLimits(max_bytes=settings.import_max_bytes, max_rows=settings.import_max_rows)


OwnerIdDep = Annotated[uuid.UUID, Depends(get_owner_id)]
ImportLimitsDep = Annotated[ImportLimits, Depends(get_import_limits)]


async def get_ledger_settings(settings: SettingsDep) -> LedgerSettings | None:
    """None when LEDGER_API_URL is unset: ledger syncs then answer 503 (ADR 0009)."""
    if settings.ledger_api_url is None:
        return None
    return LedgerSettings(
        client=LedgerConfig(
            base_url=str(settings.ledger_api_url), timeout_seconds=settings.ledger_timeout_seconds
        ),
        sync_timeout_seconds=settings.ledger_sync_timeout_seconds,
        max_entries=settings.ledger_sync_max_entries,
    )


LedgerSettingsDep = Annotated[LedgerSettings | None, Depends(get_ledger_settings)]
