"""Migrations on an empty, dedicated database.

Synchronous on purpose: migrations/env.py calls asyncio.run(), which cannot run inside the event
loop of an async test.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import date

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import URL, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from transaction_analytics.data.db import create_engine
from transaction_analytics.data.models import Base
from transaction_analytics.domain.csv_parser import ParsedTransaction
from transaction_analytics.domain.dedup import fingerprint
from transaction_analytics.domain.fields import normalize_description
from transaction_analytics.domain.money import Currency

pytestmark = pytest.mark.integration

APP_TABLES = {
    "accounts",
    "import_jobs",
    "transactions",
    "import_row_errors",
    "users",
    "api_tokens",
    "ledger_syncs",
}


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


# Rows that exist before 0003, written with raw SQL against the 0002 schema. Edge cases for the
# SQL fingerprint: multi-byte text, a zero-padded year, a negative zero-exponent amount, BIGINT max.
# (account, job, row_number, booked_on, description, amount_minor, currency)
LEGACY_ROWS = [
    ("a", 1, 2, date(2026, 9, 2), "Café ☕ \U0001f468‍\U0001f469", -320, "GBP"),
    ("a", 1, 3, date(99, 1, 1), "Old", -500, "JPY"),
    ("a", 1, 4, date(2026, 9, 3), "Big", 2**63 - 1, "USD"),
    # The same key three times in account "a" (twice in one job), once in account "b".
    ("a", 1, 5, date(2026, 9, 4), "Coffee", -100, "GBP"),
    ("a", 1, 6, date(2026, 9, 4), "Coffee", -100, "GBP"),
    ("a", 2, 2, date(2026, 9, 4), "Coffee", -100, "GBP"),
    ("b", 3, 2, date(2026, 9, 4), "Coffee", -100, "GBP"),
]
LEGACY_JOBS = {1: "a", 2: "a", 3: "b"}


def _insert_legacy_rows(connection: Connection) -> dict[str, uuid.UUID]:
    owner = uuid.uuid4()
    accounts = {
        name: connection.scalar(
            text("INSERT INTO accounts (owner_id, name) VALUES (:o, :n) RETURNING id"),
            {"o": owner, "n": name},
        )
        for name in ("a", "b")
    }
    jobs = {}
    for job, account in LEGACY_JOBS.items():
        rows = sum(1 for row in LEGACY_ROWS if row[1] == job)
        jobs[job] = connection.scalar(
            text(
                "INSERT INTO import_jobs (owner_id, account_id, status, file_sha256, total_rows,"
                " valid_rows, invalid_rows, started_at, finished_at)"
                " VALUES (:o, :a, 'completed', :sha, :n, :n, 0, now(), now()) RETURNING id"
            ),
            {"o": owner, "a": accounts[account], "sha": f"{job:064x}", "n": rows},
        )
    for account, job, row_number, booked_on, description, amount, currency in LEGACY_ROWS:
        connection.execute(
            text(
                "INSERT INTO transactions (owner_id, account_id, import_job_id, row_number,"
                " booked_on, description, normalized_description, amount_minor, currency)"
                " VALUES (:o, :a, :j, :r, :d, :desc, :norm, :amount, :cur)"
            ),
            {
                "o": owner,
                "a": accounts[account],
                "j": jobs[job],
                "r": row_number,
                "d": booked_on,
                "desc": description,
                "norm": normalize_description(description),
                "amount": amount,
                "cur": currency,
            },
        )
    connection.commit()
    return accounts


def _read_backfill(connection: Connection) -> list[tuple]:
    return list(
        connection.execute(
            text(
                "SELECT a.name, t.booked_on, t.normalized_description, t.amount_minor,"
                " t.currency, t.fingerprint, t.occurrence, j.created_at, t.row_number"
                " FROM transactions t JOIN accounts a ON a.id = t.account_id"
                " JOIN import_jobs j ON j.id = t.import_job_id"
            )
        )
    )


def test_backfilled_fingerprints_match_the_domain(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)

    command.upgrade(migration_config, "0003")

    stored = _run_sync(empty_database_url, _read_backfill)
    assert len(stored) == len(LEGACY_ROWS)
    for _, booked_on, normalized, amount, currency, stored_fingerprint, *_ in stored:
        expected = fingerprint(
            ParsedTransaction(
                row_number=2,
                booked_on=booked_on,
                description=normalized,
                normalized_description=normalized,
                amount_minor=amount,
                currency=Currency(currency),
            )
        )
        assert bytes(stored_fingerprint) == expected, (booked_on, amount, currency)


def test_backfill_numbers_existing_duplicates_per_account(
    empty_database_url: URL, migration_config: Config
) -> None:
    # Rows duplicated by pre-dedup re-imports stay, as occurrences 2, 3, ... of their key.
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)

    command.upgrade(migration_config, "0003")

    coffees = [row for row in _run_sync(empty_database_url, _read_backfill) if row[3] == -100]
    by_account: dict[str, list[int]] = {}
    for account, *_, occurrence, _created, _row in coffees:
        by_account.setdefault(account, []).append(occurrence)
    assert sorted(by_account["a"]) == [1, 2, 3]
    assert by_account["b"] == [1]
    assert len({bytes(row[5]) for row in coffees}) == 1


def _dedup_schema(connection: Connection) -> tuple[set[str], set[str], set[str]]:
    inspector = inspect(connection)
    return (
        {column["name"] for column in inspector.get_columns("transactions")},
        {uc["name"] for uc in inspector.get_unique_constraints("transactions")},
        {column["name"] for column in inspector.get_columns("import_jobs")},
    )


def test_downgrade_of_0003_restores_the_0002_schema(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)
    command.upgrade(migration_config, "head")
    columns, uniques, job_columns = _run_sync(empty_database_url, _dedup_schema)
    assert {"fingerprint", "occurrence"} <= columns
    assert "uq_transactions_dedup" in uniques
    assert "duplicate_rows" in job_columns

    command.downgrade(migration_config, "0002")

    columns, uniques, job_columns = _run_sync(empty_database_url, _dedup_schema)
    assert not {"fingerprint", "occurrence"} & columns
    assert "uq_transactions_dedup" not in uniques
    assert "duplicate_rows" not in job_columns
    stored = _run_sync(
        empty_database_url, lambda c: c.scalar(text("SELECT count(*) FROM transactions"))
    )
    assert stored == len(LEGACY_ROWS)


def _users(connection: Connection) -> dict[uuid.UUID, str]:
    return {row.id: row.name for row in connection.execute(text("SELECT id, name FROM users"))}


def _account_owner_fks(connection: Connection) -> list[str]:
    return [fk["name"] for fk in inspect(connection).get_foreign_keys("accounts")]


def test_existing_owners_become_users(empty_database_url: URL, migration_config: Config) -> None:
    # Phase 3-4 data belongs to DEV_OWNER_ID, which was never a user. It must stay reachable.
    command.upgrade(migration_config, "0002")
    accounts = _run_sync(empty_database_url, _insert_legacy_rows)  # two accounts, one owner
    owner = _run_sync(
        empty_database_url,
        lambda c: c.scalar(
            text("SELECT owner_id FROM accounts WHERE id = :a"), {"a": accounts["a"]}
        ),
    )

    command.upgrade(migration_config, "0004")

    assert _run_sync(empty_database_url, _users) == {owner: "Migrated owner"}
    assert "fk_accounts_owner" in _run_sync(empty_database_url, _account_owner_fks)


def test_downgrade_of_0004_restores_the_0003_schema(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)
    command.upgrade(migration_config, "head")

    command.downgrade(migration_config, "0003")

    assert _tables(empty_database_url) == APP_TABLES - {"users", "api_tokens", "ledger_syncs"} | {
        "alembic_version"
    }
    assert "fk_accounts_owner" not in _run_sync(empty_database_url, _account_owner_fks)
    stored = _run_sync(
        empty_database_url, lambda c: c.scalar(text("SELECT count(*) FROM transactions"))
    )
    assert stored == len(LEGACY_ROWS)


def _transaction_indexes(connection: Connection) -> set[str]:
    return {ix["name"] for ix in inspect(connection).get_indexes("transactions")}


def test_downgrade_of_0005_drops_only_the_booked_on_index(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)
    command.upgrade(migration_config, "head")
    index = "ix_transactions_owner_id_account_id_booked_on_id"
    assert index in _run_sync(empty_database_url, _transaction_indexes)

    command.downgrade(migration_config, "0004")

    assert index not in _run_sync(empty_database_url, _transaction_indexes)
    assert _tables(empty_database_url) == APP_TABLES - {"ledger_syncs"} | {"alembic_version"}
    stored = _run_sync(
        empty_database_url, lambda c: c.scalar(text("SELECT count(*) FROM transactions"))
    )
    assert stored == len(LEGACY_ROWS)


def _sources(connection: Connection) -> tuple[set[str], set[str]]:
    return (
        set(connection.scalars(text("SELECT DISTINCT source FROM accounts"))),
        set(connection.scalars(text("SELECT DISTINCT source FROM transactions"))),
    )


def test_existing_accounts_and_transactions_become_csv(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)

    command.upgrade(migration_config, "0006")

    assert _run_sync(empty_database_url, _sources) == ({"csv"}, {"csv"})


def _ledger_schema(connection: Connection) -> tuple[set[str], set[str], set[str]]:
    inspector = inspect(connection)
    return (
        {column["name"] for column in inspector.get_columns("accounts")},
        {column["name"] for column in inspector.get_columns("transactions")},
        {fk["name"] for fk in inspector.get_foreign_keys("transactions")},
    )


def test_downgrade_of_0006_restores_the_0005_schema(
    empty_database_url: URL, migration_config: Config
) -> None:
    command.upgrade(migration_config, "0002")
    _run_sync(empty_database_url, _insert_legacy_rows)
    command.upgrade(migration_config, "head")
    accounts, transactions, fks = _run_sync(empty_database_url, _ledger_schema)
    assert {"source", "ledger_user_id", "ledger_account_id"} <= accounts
    assert {"source", "ledger_sync_id", "ledger_entry_id"} <= transactions
    assert "fk_transactions_account_source" in fks
    assert "fk_transactions_account" not in fks

    command.downgrade(migration_config, "0005")

    accounts, transactions, fks = _run_sync(empty_database_url, _ledger_schema)
    assert not {"source", "ledger_user_id", "ledger_account_id"} & accounts
    assert not {"source", "ledger_sync_id", "ledger_entry_id"} & transactions
    assert "fk_transactions_account" in fks
    assert not {"fk_transactions_account_source", "fk_transactions_ledger_sync"} & fks
    assert _tables(empty_database_url) == APP_TABLES - {"ledger_syncs"} | {"alembic_version"}
    stored = _run_sync(
        empty_database_url, lambda c: c.scalar(text("SELECT count(*) FROM transactions"))
    )
    assert stored == len(LEGACY_ROWS)


def _add_ledger_account(connection: Connection) -> None:
    owner = connection.scalar(text("INSERT INTO users (name) VALUES ('Ledger owner') RETURNING id"))
    connection.execute(
        text(
            "INSERT INTO accounts (owner_id, name, source, ledger_user_id, ledger_account_id)"
            " VALUES (:o, 'Ledger', 'ledger', gen_random_uuid(), gen_random_uuid())"
        ),
        {"o": owner},
    )
    connection.commit()


def test_downgrade_of_0006_refuses_while_ledger_accounts_exist(
    empty_database_url: URL, migration_config: Config
) -> None:
    # Their transactions have no import job, and a migration never deletes financial rows.
    command.upgrade(migration_config, "head")
    _run_sync(empty_database_url, _add_ledger_account)

    with pytest.raises(Exception, match="ledger accounts exist"):
        command.downgrade(migration_config, "0005")

    version = _run_sync(
        empty_database_url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version"))
    )
    assert version == "0006"
    assert "ledger_syncs" in _tables(empty_database_url)
