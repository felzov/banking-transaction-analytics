from fastapi import APIRouter

from transaction_analytics.api.deps import CurrentUserDep
from transaction_analytics.api.schemas import UserOut

router = APIRouter(tags=["users"])


@router.get("/me")
async def get_me(user: CurrentUserDep) -> UserOut:
    """The user the API token belongs to (loaded by the token lookup itself)."""
    return UserOut.of(user)
