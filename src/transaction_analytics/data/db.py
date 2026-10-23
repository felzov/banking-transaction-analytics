"""Engine and session factories. Nothing is created at import time; main.py lifespan wires them."""

import math

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import Pool


def connect_args(
    *, statement_timeout_seconds: float | None, connect_timeout_seconds: float | None
) -> dict[str, object]:
    """asyncpg connection arguments for the limits that are set (ADR 0010)."""
    args: dict[str, object] = {}
    if statement_timeout_seconds is not None:
        # Server side: PostgreSQL cancels the statement and the connection stays usable. In ms,
        # rounded up: 0 would mean "no limit" to PostgreSQL.
        milliseconds = math.ceil(statement_timeout_seconds * 1000)
        args["server_settings"] = {"statement_timeout": str(milliseconds)}
    if connect_timeout_seconds is not None:
        args["timeout"] = connect_timeout_seconds
    return args


def create_engine(
    url: str,
    *,
    poolclass: type[Pool] | None = None,
    statement_timeout_seconds: float | None = None,
    connect_timeout_seconds: float | None = None,
) -> AsyncEngine:
    """Limits are off unless given: only the app sets them. Migrations (index builds) and the CLI
    run unbounded."""
    options: dict[str, object] = {
        "pool_pre_ping": True,
        # SQLAlchemy puts bound parameters (descriptions, amounts) into exception text by default.
        # Those exceptions can reach logs, so they must never carry statement data.
        "hide_parameters": True,
    }
    if poolclass is not None:
        options["poolclass"] = poolclass
    args = connect_args(
        statement_timeout_seconds=statement_timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
    )
    if args:
        options["connect_args"] = args
    return create_async_engine(url, **options)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: attributes stay readable after commit without an implicit
    # lazy reload, which in async code would fail outside an awaited context.
    return async_sessionmaker(engine, expire_on_commit=False)
