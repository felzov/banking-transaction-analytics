from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from transaction_analytics.api.accounts import router as accounts_router
from transaction_analytics.api.errors import install_error_handling
from transaction_analytics.api.health import router as health_router
from transaction_analytics.api.imports import router as imports_router
from transaction_analytics.api.ledger import router as ledger_router
from transaction_analytics.api.limits import RequestSizeLimitMiddleware
from transaction_analytics.api.transactions import router as transactions_router
from transaction_analytics.api.users import router as users_router
from transaction_analytics.data.db import create_engine, create_sessionmaker
from transaction_analytics.log_config import configure_logging
from transaction_analytics.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Settings and the engine are created on startup, not at import time, so importing the app
    # (unit tests, tooling) needs no DATABASE_URL.
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(
        settings.database_url.get_secret_value(),
        statement_timeout_seconds=settings.db_statement_timeout_seconds,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
    )
    app.state.sessionmaker = create_sessionmaker(engine)
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Banking Transaction Analytics", version="0.1.0", lifespan=lifespan)
    # add_middleware wraps what is already there, so the error middleware installed next ends up
    # outermost and also covers a failure of the size limit.
    app.add_middleware(RequestSizeLimitMiddleware)
    install_error_handling(app)
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(accounts_router)
    app.include_router(imports_router)
    app.include_router(ledger_router)
    app.include_router(transactions_router)
    return app


app = create_app()
