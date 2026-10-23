"""Connection limits of the app's engine (ADR 0010). The statement timeout against a real server is
in tests/integration/test_database.py."""

import socket
import time
from collections.abc import Iterator

import pytest
from sqlalchemy.pool import NullPool

from transaction_analytics.data.db import connect_args, create_engine


def test_no_limits_by_default() -> None:
    # Migrations and the CLI create engines without limits: an index build may take long.
    assert connect_args(statement_timeout_seconds=None, connect_timeout_seconds=None) == {}


def test_limits_become_asyncpg_arguments() -> None:
    assert connect_args(statement_timeout_seconds=30, connect_timeout_seconds=5) == {
        "server_settings": {"statement_timeout": "30000"},
        "timeout": 5,
    }


@pytest.mark.parametrize(("seconds", "milliseconds"), [(0.0001, "1"), (0.0015, "2"), (2.5, "2500")])
def test_statement_timeout_rounds_up_to_whole_milliseconds(
    seconds: float, milliseconds: str
) -> None:
    # Rounding down could reach 0, which PostgreSQL reads as "no limit".
    args = connect_args(statement_timeout_seconds=seconds, connect_timeout_seconds=None)

    assert args == {"server_settings": {"statement_timeout": milliseconds}}


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A port that accepts connections (the kernel completes the handshake) but never answers."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener.getsockname()[1]


async def test_connect_timeout_bounds_a_server_that_never_answers(silent_server: int) -> None:
    engine = create_engine(
        f"postgresql+asyncpg://user:password@127.0.0.1:{silent_server}/db",
        poolclass=NullPool,
        connect_timeout_seconds=0.2,
    )
    started = time.monotonic()
    try:
        # asyncpg's TimeoutError (an OSError) is not wrapped in a DBAPIError.
        with pytest.raises(TimeoutError):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()

    assert time.monotonic() - started < 5  # asyncpg's own default would be 60 s
