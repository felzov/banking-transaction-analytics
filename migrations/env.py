"""Alembic environment (async, asyncpg).

URL source, in order: `config.attributes["database_url"]` (set by the tests) or DATABASE_URL via
Settings. The URL is never put into alembic.ini or `set_main_option`: configparser would try to
interpolate a `%` in a percent-encoded password.

`asyncio.run()` below means Alembic commands must not be called from inside a running event loop.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from transaction_analytics.data.db import create_engine
from transaction_analytics.data.models import Base
from transaction_analytics.settings import get_settings

config = context.config

if config.config_file_name is not None:
    # Keep loggers configured by the host process (pytest, the app) working.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if url is None:
        url = get_settings().database_url.get_secret_value()
    return url


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_migrations_online())
