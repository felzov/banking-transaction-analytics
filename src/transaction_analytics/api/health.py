from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up. Deliberately does not check the database (readiness,
    including the database, is Phase 8)."""
    return {"status": "ok"}
