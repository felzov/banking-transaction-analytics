from fastapi import APIRouter, Response

from transaction_analytics.api.deps import SessionDep
from transaction_analytics.services import readiness as readiness_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only: the process is up. Deliberately does not check the database, so a database
    outage never gets the process restarted; that is what /ready reports (ADR 0010)."""
    return {"status": "ok"}


@router.get(
    "/ready",
    responses={
        503: {
            "description": "Not ready: the database is unreachable or its schema is not "
            "at this code's revision. The reason is in the server log only."
        }
    },
)
async def ready(session: SessionDep, response: Response) -> dict[str, str]:
    """Readiness: the database answers and is migrated to this code's revision. No token needed."""
    await readiness_service.check_ready(session)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ready"}
