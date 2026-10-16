"""Engine and session factories. Nothing is created at import time; Phase 3 wires them in."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import Pool


def create_engine(url: str, *, poolclass: type[Pool] | None = None) -> AsyncEngine:
    options: dict[str, object] = {
        "pool_pre_ping": True,
        # SQLAlchemy puts bound parameters (descriptions, amounts) into exception text by default.
        # Those exceptions can reach logs, so they must never carry statement data.
        "hide_parameters": True,
    }
    if poolclass is not None:
        options["poolclass"] = poolclass
    return create_async_engine(url, **options)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: attributes stay readable after commit without an implicit
    # lazy reload, which in async code would fail outside an awaited context.
    return async_sessionmaker(engine, expire_on_commit=False)
