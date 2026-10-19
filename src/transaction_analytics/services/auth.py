"""Users and API tokens (ADR 0007).

A token's text exists only in the IssuedToken that issue_token returns (and then with the client);
the database holds its SHA-256. authenticate is the only lookup not scoped by an owner: it is how a
request's owner is found.
"""

import secrets
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from transaction_analytics.data import auth as auth_data
from transaction_analytics.data.models import ApiToken, User
from transaction_analytics.domain.auth import (
    TOKEN_PREFIX,
    TOKEN_RANDOM_BYTES,
    check_token_days,
    is_well_formed,
    token_digest,
)
from transaction_analytics.domain.labels import clean_label
from transaction_analytics.services.errors import (
    NotAuthenticatedError,
    TokenNotFoundError,
    UserNotFoundError,
)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: ApiToken
    # Kept out of repr(), so logging the result by mistake does not log the token.
    plaintext: str = field(repr=False)


def generate_token() -> str:
    # token_urlsafe(32) is 43 base64url characters without padding: the shape is_well_formed wants.
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_RANDOM_BYTES)


async def authenticate(session: AsyncSession, *, token: str | None) -> uuid.UUID:
    """The owner of a live token; NotAuthenticatedError for anything else.

    A malformed token is refused without a query. After the lookup the read transaction is ended:
    the upload route streams a body of up to 5 MiB after authenticating, and must not hold a pooled
    connection "idle in transaction" meanwhile (ADR 0004, item 10).
    """
    if token is None or not is_well_formed(token):
        raise NotAuthenticatedError
    owner_id = await auth_data.find_token_owner(session, token_sha256=token_digest(token))
    await session.rollback()
    if owner_id is None:
        raise NotAuthenticatedError
    return owner_id


async def create_user(session: AsyncSession, *, name: str) -> User:
    """Raises LabelValidationError for an invalid name."""
    user = await auth_data.add_user(session, name=clean_label(name))
    await session.commit()
    return user


async def get_user(session: AsyncSession, *, owner_id: uuid.UUID) -> User:
    user = await auth_data.get_user(session, owner_id=owner_id)
    if user is None:
        raise UserNotFoundError
    return user


async def disable_user(session: AsyncSession, *, owner_id: uuid.UUID) -> None:
    """Refuses the user's tokens from now on; their data stays."""
    if not await auth_data.disable_user(session, owner_id=owner_id):
        raise UserNotFoundError
    await session.commit()


async def issue_token(
    session: AsyncSession, *, owner_id: uuid.UUID, name: str, days: int
) -> IssuedToken:
    """Raises LabelValidationError, TokenValidationError or UserNotFoundError."""
    name = clean_label(name)
    check_token_days(days)
    await get_user(session, owner_id=owner_id)
    plaintext = generate_token()
    token = await auth_data.add_token(
        session, owner_id=owner_id, name=name, token_sha256=token_digest(plaintext), days=days
    )
    await session.commit()
    return IssuedToken(token=token, plaintext=plaintext)


async def list_tokens(session: AsyncSession, *, owner_id: uuid.UUID) -> list[ApiToken]:
    await get_user(session, owner_id=owner_id)
    return await auth_data.list_tokens(session, owner_id=owner_id)


async def revoke_token(session: AsyncSession, *, owner_id: uuid.UUID, token_id: uuid.UUID) -> None:
    if not await auth_data.revoke_token(session, owner_id=owner_id, token_id=token_id):
        raise TokenNotFoundError
    await session.commit()
