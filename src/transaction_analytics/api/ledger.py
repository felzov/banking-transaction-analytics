import uuid

from fastapi import APIRouter, Request, Response

from transaction_analytics.api.deps import LedgerSettingsDep, OwnerIdDep, SessionDep
from transaction_analytics.api.schemas import LedgerSyncCreate, LedgerSyncOut
from transaction_analytics.services import ledger_sync as ledger_sync_service

router = APIRouter(tags=["ledger"])


@router.post(
    "/accounts/{account_id}/ledger-syncs",
    status_code=201,
    # Explicit: the annotation includes Response, returned as is for 204.
    response_model=LedgerSyncOut,
    responses={
        204: {"description": "Nothing new in the ledger; no sync was recorded."},
    },
)
async def sync_ledger_account(
    account_id: uuid.UUID,
    body: LedgerSyncCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    owner_id: OwnerIdDep,
    ledger: LedgerSettingsDep,
) -> LedgerSyncOut | Response:
    """Read the linked ledger account's statement and store the entries this account lacks.

    201 with the recorded sync when there were new entries; 204 when there were none.
    """
    result = await ledger_sync_service.sync_account(
        session,
        owner_id=owner_id,
        account_id=account_id,
        token=body.ledger_token.get_secret_value(),
        ledger=ledger,
    )
    if result.sync is None:
        return Response(status_code=204)
    url = request.url_for("get_ledger_sync", sync_id=str(result.sync.id))
    response.headers["Location"] = str(url)
    return LedgerSyncOut.of(result.sync)


@router.get("/ledger-syncs/{sync_id}")
async def get_ledger_sync(
    sync_id: uuid.UUID, session: SessionDep, owner_id: OwnerIdDep
) -> LedgerSyncOut:
    sync = await ledger_sync_service.get_sync(session, owner_id=owner_id, sync_id=sync_id)
    return LedgerSyncOut.of(sync)
