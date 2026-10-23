"""Whether this instance should get traffic (ADR 0010).

Ready means the database answers and its schema is at the revision this code expects. It is not
during a deploy before `alembic upgrade head`, nor after the app was rolled back below a migration.

The ledger is not checked. It is optional, and its outage already answers 502 on ledger syncs
alone; counting it here would take CSV imports and analytics out of service with it.
"""

import asyncio
import logging
from typing import NoReturn

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import schema as schema_data
from transaction_analytics.data.errors import error_kind
from transaction_analytics.services.errors import NotReadyError

logger = logging.getLogger(__name__)

# For the whole check, including the wait for a pooled connection. Read at call time (tests).
READY_TIMEOUT_SECONDS = 2.0


def _not_ready(reason: str, *args: object) -> NoReturn:
    # The reason goes to the log only: the probe needs no token, so it tells callers nothing.
    logger.warning("Not ready: " + reason, *args)
    raise NotReadyError from None


async def check_ready(session: AsyncSession) -> None:
    """Returns when ready; raises NotReadyError otherwise."""
    try:
        async with asyncio.timeout(READY_TIMEOUT_SECONDS):
            revisions = await schema_data.schema_revisions(session)
    except TimeoutError:  # before OSError: asyncio's and asyncpg's timeouts are OSErrors too
        _not_ready("the database did not answer within %s s", READY_TIMEOUT_SECONDS)
    except DBAPIError as exc:
        _not_ready("database error (%s)", error_kind(exc))
    except OSError as exc:
        # Refused connections and unknown hosts are not wrapped in DBAPIError; their text names the
        # host and port, so only the class is logged.
        _not_ready("cannot reach the database (%s)", type(exc).__name__)
    if revisions != [schema_data.EXPECTED_SCHEMA_REVISION]:
        _not_ready(
            "database schema is at %s, this code expects %s",
            revisions or "no revision",
            schema_data.EXPECTED_SCHEMA_REVISION,
        )
