from fastapi import APIRouter

from transaction_analytics.api.deps import OwnerIdDep, SessionDep
from transaction_analytics.api.schemas import UserOut
from transaction_analytics.services import auth as auth_service

router = APIRouter(tags=["users"])


@router.get("/me")
async def get_me(session: SessionDep, owner_id: OwnerIdDep) -> UserOut:
    """The user the API token belongs to."""
    user = await auth_service.get_user(session, owner_id=owner_id)
    return UserOut.of(user)
