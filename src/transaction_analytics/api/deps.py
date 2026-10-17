"""Request dependencies. Tests replace these through app.dependency_overrides.

All are `async def`: FastAPI runs a sync dependency in its threadpool, which would cost a thread
hop per request just to return a ready value (and compete with the parser's worker threads).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.services.imports import ImportLimits
from transaction_analytics.settings import Settings, get_settings


async def get_app_settings() -> Settings:
    return get_settings()  # cached after the first call


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # The sessionmaker is created in the app lifespan (main.py). Services commit explicitly;
    # closing the session rolls back anything left uncommitted.
    async with request.app.state.sessionmaker() as session:
        yield session


async def get_owner_id(settings: SettingsDep) -> uuid.UUID:
    """Placeholder until authentication (Phase 5): every request acts as one configured owner."""
    return settings.dev_owner_id


async def get_import_limits(settings: SettingsDep) -> ImportLimits:
    return ImportLimits(max_bytes=settings.import_max_bytes, max_rows=settings.import_max_rows)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
OwnerIdDep = Annotated[uuid.UUID, Depends(get_owner_id)]
ImportLimitsDep = Annotated[ImportLimits, Depends(get_import_limits)]
