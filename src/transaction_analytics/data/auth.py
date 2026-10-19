"""User and API token persistence (ADR 0007).

Every function requires owner_id (a user id) except two: add_user, which creates an owner, and
find_token_owner, which is how a request's owner is found in the first place, from its token.
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data.models import ApiToken, User


async def add_user(session: AsyncSession, *, name: str) -> User:
    statement = insert(User).values(name=name).returning(User)
    return (await session.scalars(statement)).one()


async def get_user(session: AsyncSession, *, owner_id: uuid.UUID) -> User | None:
    return await session.scalar(select(User).where(User.id == owner_id))


async def disable_user(session: AsyncSession, *, owner_id: uuid.UUID) -> bool:
    """False if there is no such user. Disabling again keeps the first time."""
    statement = (
        update(User)
        .where(User.id == owner_id)
        .values(disabled_at=func.coalesce(User.disabled_at, func.now()))
        .returning(User.id)
    )
    return await session.scalar(statement) is not None


async def add_token(
    session: AsyncSession, *, owner_id: uuid.UUID, name: str, token_sha256: bytes, days: int
) -> ApiToken:
    statement = (
        insert(ApiToken)
        .values(
            owner_id=owner_id,
            name=name,
            token_sha256=token_sha256,
            # Database clock, like every other timestamp (ADR 0003) and the expiry check below.
            expires_at=func.now() + timedelta(days=days),
        )
        .returning(ApiToken)
    )
    return (await session.scalars(statement)).one()


async def find_token_owner(session: AsyncSession, *, token_sha256: bytes) -> uuid.UUID | None:
    """The owner of a token that is live: not revoked, not expired, its user not disabled.

    Not scoped by owner_id: this lookup is what determines the owner. Served by the unique index on
    token_sha256.
    """
    statement = (
        select(ApiToken.owner_id)
        .join(User, User.id == ApiToken.owner_id)
        .where(
            ApiToken.token_sha256 == token_sha256,
            ApiToken.revoked_at.is_(None),
            ApiToken.expires_at > func.now(),
            User.disabled_at.is_(None),
        )
    )
    return await session.scalar(statement)


async def list_tokens(session: AsyncSession, *, owner_id: uuid.UUID) -> list[ApiToken]:
    statement = (
        select(ApiToken)
        .where(ApiToken.owner_id == owner_id)
        .order_by(ApiToken.created_at, ApiToken.id)
    )
    return list(await session.scalars(statement))


async def revoke_token(session: AsyncSession, *, owner_id: uuid.UUID, token_id: uuid.UUID) -> bool:
    """False if the owner has no such token. Revoking again keeps the first time."""
    statement = (
        update(ApiToken)
        .where(ApiToken.owner_id == owner_id, ApiToken.id == token_id)
        .values(revoked_at=func.coalesce(ApiToken.revoked_at, func.now()))
        .returning(ApiToken.id)
    )
    return await session.scalar(statement) is not None
