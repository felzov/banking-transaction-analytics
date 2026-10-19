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
    UserDisabledError,
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


async def authenticate(session: AsyncSession, *, token: str | None) -> User:
    """The user of a live token; NotAuthenticatedError for anything else.

    A malformed token is refused without a query. The lookup's read transaction stays open for the
    rest of the request, which uses the same session; the upload route ends it before streaming
    its body (services.imports.release_connection).
    """
    if token is None or not is_well_formed(token):
        raise NotAuthenticatedError(token_sent=token is not None)
    user = await auth_data.find_token_user(session, token_sha256=token_digest(token))
    if user is None:
        raise NotAuthenticatedError(token_sent=True)
    return user


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
    """Raises LabelValidationError, TokenValidationError, UserNotFoundError or
    UserDisabledError (its token could never be used)."""
    name = clean_label(name)
    check_token_days(days)
    if (await get_user(session, owner_id=owner_id)).disabled_at is not None:
        raise UserDisabledError
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
    # A mistyped user id must not read as "token not found": that would suggest a leaked token
    # is already gone.
    await get_user(session, owner_id=owner_id)
    if not await auth_data.revoke_token(session, owner_id=owner_id, token_id=token_id):
        raise TokenNotFoundError
    await session.commit()
