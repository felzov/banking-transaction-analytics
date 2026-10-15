from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up. Does not check the database (none is used yet)."""
    return {"status": "ok"}
