import uuid

from fastapi import APIRouter

from transaction_analytics.api.deps import OwnerIdDep, SessionDep
from transaction_analytics.api.schemas import AccountCreate, AccountOut
from transaction_analytics.services import accounts as account_service

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", status_code=201)
async def create_account(
    body: AccountCreate, session: SessionDep, owner_id: OwnerIdDep
) -> AccountOut:
    account = await account_service.create_account(session, owner_id=owner_id, name=body.name)
    return AccountOut.of(account)


@router.get("")
async def list_accounts(session: SessionDep, owner_id: OwnerIdDep) -> list[AccountOut]:
    accounts = await account_service.list_accounts(session, owner_id=owner_id)
    return [AccountOut.of(account) for account in accounts]


@router.get("/{account_id}")
async def get_account(
    account_id: uuid.UUID, session: SessionDep, owner_id: OwnerIdDep
) -> AccountOut:
    account = await account_service.get_account(session, owner_id=owner_id, account_id=account_id)
    return AccountOut.of(account)
