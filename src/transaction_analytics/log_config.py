"""Logging for the app's own loggers (ADR 0010).

uvicorn configures only its own loggers (uvicorn, uvicorn.error, uvicorn.access). Without this,
`transaction_analytics.*` records fall through to Python's last-resort handler: WARNING and above
only, as a bare message without time, level or logger name.

Only the `transaction_analytics` logger gets a handler; the root logger is left to the host
process (pytest's capture, a server's own setup), and records still propagate to it.
"""

import logging
import sys

APP_LOGGER = "transaction_analytics"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_HANDLER_NAME = "transaction_analytics.stdout"


def configure_logging(level: str) -> None:
    """Idempotent: a second call replaces the handler instead of adding another one."""
    logger = logging.getLogger(APP_LOGGER)
    for handler in [h for h in logger.handlers if h.get_name() == _HANDLER_NAME]:
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    # At INFO, SQLAlchemy's engine logs statement parameters: descriptions and amounts.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
