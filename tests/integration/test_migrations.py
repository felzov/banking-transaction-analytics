"""Migrations on an empty, dedicated database.

Synchronous on purpose: migrations/env.py calls asyncio.run(), which cannot run inside the event
loop of an async test.
"""

import asyncio
from collections.abc import Callable

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import URL, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from transaction_analytics.data.db import create_engine
from transaction_analytics.data.models import Base

pytestmark = pytest.mark.integration

APP_TABLES = {"accounts", "import_jobs", "transactions", "import_row_errors"}


def _run_sync[T](url: URL, function: Callable[[Connection], T]) -> T:
    async def run() -> T:
        engine = create_engine(url.render_as_string(hide_password=False), poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(function)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _tables(url: URL) -> set[str]:
    return _run_sync(url, lambda connection: set(inspect(connection).get_table_names()))


def test_upgrade_downgrade_upgrade(empty_database_url: URL, migration_config: Config) -> None:
    command.upgrade(migration_config, "head")
    assert _tables(empty_database_url) == APP_TABLES | {"alembic_version"}

    command.downgrade(migration_config, "base")
    assert _tables(empty_database_url) == {"alembic_version"}

    command.upgrade(migration_config, "head")
    assert _tables(empty_database_url) == APP_TABLES | {"alembic_version"}


def test_models_match_migrations(empty_database_url: URL, migration_config: Config) -> None:
    # Catches a model change without a migration (and vice versa), including server defaults.
    # Autogenerate does not compare CHECK constraints; test_constraints.py covers those.
    command.upgrade(migration_config, "head")

    diff = _run_sync(
        empty_database_url,
        lambda connection: compare_metadata(
            MigrationContext.configure(connection, opts={"compare_server_default": True}),
            Base.metadata,
        ),
    )

    assert diff == []


def _import_job_uniques(connection: Connection) -> tuple[set[str], set[str]]:
    inspector = inspect(connection)
    constraints = {uc["name"] for uc in inspector.get_unique_constraints("import_jobs")}
    unique_indexes = {
        ix["name"]
        for ix in inspector.get_indexes("import_jobs")
        if ix["unique"] and not ix.get("duplicates_constraint")
    }
    return constraints, unique_indexes


def _account_name_check(connection: Connection) -> str:
    (check,) = [
        ck
        for ck in inspect(connection).get_check_constraints("accounts")
        if ck["name"] == "ck_accounts_name_valid"
    ]
    return check["sqltext"]


def test_downgrade_of_0002_restores_the_0001_schema(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "head")
    constraints, unique_indexes = _run_sync(empty_database_url, _import_job_uniques)
    assert "uq_import_jobs_active_file_sha256" in unique_indexes
    assert "uq_import_jobs_owner_id_account_id_file_sha256" not in constraints
    assert "[[:cntrl:]]" in _run_sync(empty_database_url, _account_name_check)

    command.downgrade(migration_config, "0001")

    constraints, unique_indexes = _run_sync(empty_database_url, _import_job_uniques)
    assert "uq_import_jobs_owner_id_account_id_file_sha256" in constraints
    assert "uq_import_jobs_active_file_sha256" not in unique_indexes
    old_check = _run_sync(empty_database_url, _account_name_check)
    assert "btrim(name) <> ''" in old_check
    assert "[[:cntrl:]]" not in old_check
