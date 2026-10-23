"""Fixtures for every test directory."""

import logging
from collections.abc import Iterator

import pytest

from transaction_analytics.log_config import APP_LOGGER


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    # A test that runs the app's lifespan configures logging (log_config.configure_logging). Its
    # handler writes to that test's captured stdout, so it must not outlive the test.
    watched = [logging.getLogger(APP_LOGGER), logging.getLogger("sqlalchemy.engine")]
    saved = [(logger, list(logger.handlers), logger.level) for logger in watched]
    yield
    for logger, handlers, level in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
