"""What the app's lifespan builds from settings, without a database (ADR 0010)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from transaction_analytics import main


async def test_the_lifespan_passes_the_database_limits_to_the_engine(
    app_environment: pytest.MonkeyPatch,
) -> None:
    # Review fix (M1): dropping the limits from the lifespan left every test green. The engine
    # tests (unit/test_db.py, integration/test_database.py) only cover create_engine itself.
    app_environment.setenv("DB_STATEMENT_TIMEOUT_SECONDS", "12.5")
    app_environment.setenv("DB_CONNECT_TIMEOUT_SECONDS", "3")
    received: dict[str, Any] = {}
    real = main.create_engine

    def recording(url: str, **options: Any) -> AsyncEngine:
        received.update(options)
        return real(url, **options)

    app_environment.setattr(main, "create_engine", recording)
    app = main.create_app()

    async with app.router.lifespan_context(app):
        pass

    assert received == {"statement_timeout_seconds": 12.5, "connect_timeout_seconds": 3}
