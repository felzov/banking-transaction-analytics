"""What a database error does and does not reveal about the row that caused it.

Statement rows are personal financial data (README "Errors", ADR 0003). The results here are why
ADR 0003 forbids logging or returning any DBAPIError via str()/repr():

- Server-side constraint violations: SQLAlchemy's text is clean thanks to hide_parameters=True,
  but asyncpg's `detail` carries the whole failing row.
- Client-side encoding errors (asyncpg rejects a parameter before it reaches the server): the
  value is part of the exception message itself, and hide_parameters cannot remove it.

Only the exception class name and constraint_name are safe to log.
"""

import pytest
from factories import account, completed, transaction
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.errors import constraint_name
from transaction_analytics.data.models import ImportJob

pytestmark = pytest.mark.integration

MARKER = "LEAK-ME-1234"
# One past the BIGINT range. The domain never produces it (MAX_ABS_AMOUNT_MINOR); a bug could.
OUT_OF_RANGE_AMOUNT = 2**63


async def _import_job(session: AsyncSession) -> ImportJob:
    acc = account()
    session.add(acc)
    await session.flush()
    import_job = completed(acc)
    session.add(import_job)
    await session.flush()
    return import_job


async def _violate_check_with_marker(session: AsyncSession) -> IntegrityError:
    import_job = await _import_job(session)

    with pytest.raises(IntegrityError) as exc:
        async with session.begin_nested():
            session.add(
                transaction(
                    import_job,
                    description=f"Payment {MARKER}",
                    normalized_description=f"payment {MARKER.casefold()}",
                    currency="XYZ",
                )
            )
            await session.flush()
    assert constraint_name(exc.value) == "ck_transactions_currency_allowed"
    return exc.value


async def test_sqlalchemy_error_text_does_not_contain_row_values(session: AsyncSession) -> None:
    error = await _violate_check_with_marker(session)

    for rendered in (str(error), repr(error)):
        assert MARKER not in rendered
        assert MARKER.casefold() not in rendered


async def test_asyncpg_detail_does_contain_row_values(session: AsyncSession) -> None:
    # Documents the risk instead of hiding it: anything that logs exc.orig / its cause, or
    # Postgres's own server log, sees the row.
    error = await _violate_check_with_marker(session)

    detail = getattr(error.orig.__cause__, "detail", None) or ""
    assert "Failing row contains" in detail
    assert MARKER in detail


async def test_client_side_data_error_text_does_contain_the_value(session: AsyncSession) -> None:
    # Documents the risk instead of hiding it: hide_parameters=True does not help here, so even
    # str(exc) of the SQLAlchemy wrapper must never be logged or returned.
    import_job = await _import_job(session)

    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            session.add(transaction(import_job, amount_minor=OUT_OF_RANGE_AMOUNT))
            await session.flush()

    assert not isinstance(exc.value, IntegrityError)  # constraint-name mapping won't catch it
    assert type(exc.value.orig.__cause__).__name__ == "DataError"
    assert constraint_name(exc.value) is None
    assert str(OUT_OF_RANGE_AMOUNT) in str(exc.value)
    assert str(OUT_OF_RANGE_AMOUNT) in repr(exc.value)
