"""The readiness check (ADR 0010) without a database: the data function is replaced."""

import asyncio
import logging
from collections.abc import Callable

import pytest
from sqlalchemy.exc import DBAPIError

from transaction_analytics.data import schema as schema_data
from transaction_analytics.data.schema import EXPECTED_SCHEMA_REVISION
from transaction_analytics.services import readiness
from transaction_analytics.services.errors import NotReadyError

MARKER = "LEAK-ME-7890"
SESSION = object()  # never used: the data function is replaced


def _revisions(result: list[str] | BaseException) -> Callable[[object], object]:
    async def revisions(session: object) -> list[str]:
        assert session is SESSION
        if isinstance(result, BaseException):
            raise result
        return result

    return revisions


async def test_ready_when_the_schema_is_at_the_expected_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schema_data, "schema_revisions", _revisions([EXPECTED_SCHEMA_REVISION]))

    await readiness.check_ready(SESSION)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "revisions",
    [
        [],  # never migrated
        ["0005"],  # deployed before `alembic upgrade head`
        ["9999"],  # the app was rolled back below a migration
        [EXPECTED_SCHEMA_REVISION, "9999"],  # branched heads
    ],
    ids=["none", "behind", "unknown", "two"],
)
async def test_not_ready_unless_exactly_at_the_expected_revision(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, revisions: list[str]
) -> None:
    monkeypatch.setattr(schema_data, "schema_revisions", _revisions(revisions))

    with caplog.at_level(logging.WARNING), pytest.raises(NotReadyError):
        await readiness.check_ready(SESSION)  # type: ignore[arg-type]

    assert f"this code expects {EXPECTED_SCHEMA_REVISION}" in caplog.text


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (
            DBAPIError("SELECT ...", {"x": MARKER}, Exception(f"value {MARKER}")),
            "database error (DBAPIError)",
        ),
        (
            ConnectionRefusedError(f"Connect call failed ('{MARKER}', 5432)"),
            "cannot reach the database (ConnectionRefusedError)",
        ),
        (
            OSError(f"Multiple exceptions: {MARKER}"),
            "cannot reach the database (OSError)",
        ),
    ],
    ids=["dbapi", "refused", "oserror"],
)
async def test_database_failures_are_logged_by_class_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    logged: str,
) -> None:
    # Connection errors name the host and port; DBAPIError text can carry row values.
    monkeypatch.setattr(schema_data, "schema_revisions", _revisions(error))

    with caplog.at_level(logging.DEBUG), pytest.raises(NotReadyError) as exc:
        await readiness.check_ready(SESSION)  # type: ignore[arg-type]

    assert logged in caplog.text
    assert MARKER not in caplog.text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


async def test_a_database_that_does_not_answer_is_not_ready_in_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def hangs(session: object) -> list[str]:
        await asyncio.sleep(60)
        return [EXPECTED_SCHEMA_REVISION]

    monkeypatch.setattr(schema_data, "schema_revisions", hangs)
    monkeypatch.setattr(readiness, "READY_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level(logging.WARNING), pytest.raises(NotReadyError):
        await asyncio.wait_for(readiness.check_ready(SESSION), timeout=5)  # type: ignore[arg-type]

    assert "did not answer within 0.05 s" in caplog.text


async def test_a_connect_timeout_counts_as_not_answering(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # asyncpg's connect timeout is a TimeoutError, which is also an OSError.
    monkeypatch.setattr(schema_data, "schema_revisions", _revisions(TimeoutError()))

    with caplog.at_level(logging.WARNING), pytest.raises(NotReadyError):
        await readiness.check_ready(SESSION)  # type: ignore[arg-type]

    assert "did not answer" in caplog.text


async def test_a_bug_is_not_reported_as_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only database failures mean "not ready"; anything else is a 500 like elsewhere.
    monkeypatch.setattr(schema_data, "schema_revisions", _revisions(KeyError("bug")))

    with pytest.raises(KeyError):
        await readiness.check_ready(SESSION)  # type: ignore[arg-type]
