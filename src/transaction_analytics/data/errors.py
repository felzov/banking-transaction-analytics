"""What may be learned from a database error without leaking row values (ADR 0003, item 10).

Never log or return a DBAPIError via str()/repr(): its text, or that of exc.orig and its cause,
can contain statement data. Only the two values below are safe.
"""

from sqlalchemy.exc import DBAPIError


def _driver_error(exc: DBAPIError) -> BaseException | None:
    # SQLAlchemy wraps the asyncpg adapter's exception (exc.orig), which wraps asyncpg's own.
    return getattr(exc.orig, "__cause__", None)


def constraint_name(exc: DBAPIError) -> str | None:
    """Name of the violated constraint or unique index, if the server reported one."""
    return getattr(_driver_error(exc), "constraint_name", None)


def error_kind(exc: DBAPIError) -> str:
    """Driver exception class name, e.g. "UniqueViolationError" or "DataError"."""
    driver_error = _driver_error(exc)
    return type(driver_error if driver_error is not None else exc).__name__
