"""The operator CLI against a real database, with real COMMITs (committed_database_url).

Each command runs in its own session, like separate invocations; authenticate is then checked in
another fresh session, as a request would.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from factories import OWNER_ID
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from transaction_analytics import cli
from transaction_analytics.data.db import create_engine, create_sessionmaker
from transaction_analytics.data.models import ApiToken, User
from transaction_analytics.domain.auth import is_well_formed, token_digest
from transaction_analytics.services import auth as auth_service
from transaction_analytics.services.errors import NotAuthenticatedError

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions(committed_database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(committed_database_url, poolclass=NullPool)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def run(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    status = await cli.run(cli.parse_args(argv), sessions)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


async def authenticate(sessions: async_sessionmaker[AsyncSession], token: str) -> uuid.UUID:
    async with sessions() as session:
        return await auth_service.authenticate(session, token=token)


async def count(sessions: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessions() as session:
        return (await session.scalar(select(func.count()).select_from(model))) or 0


async def test_create_issue_list_revoke(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = await run(sessions, capsys, "create-user", "--name", "  Max ")
    assert status == 0
    user_id = uuid.UUID(out.strip())

    status, out, err = await run(
        sessions, capsys, "issue-token", "--user-id", str(user_id), "--name", "laptop"
    )
    assert status == 0
    token = out.strip()
    assert out == token + "\n"  # stdout is the token alone, for scripts
    assert is_well_formed(token)
    assert "shown only now" in err
    assert token not in err
    assert await authenticate(sessions, token) == user_id

    status, out, _ = await run(sessions, capsys, "list-tokens", "--user-id", str(user_id))
    (line,) = out.splitlines()
    token_id, state, _, name = line.split("\t")
    assert (state, name) == ("active", "laptop")
    assert token not in out

    status, _, _ = await run(
        sessions, capsys, "revoke-token", "--user-id", str(user_id), "--token-id", token_id
    )
    assert status == 0
    with pytest.raises(NotAuthenticatedError):
        await authenticate(sessions, token)
    _, out, _ = await run(sessions, capsys, "list-tokens", "--user-id", str(user_id))
    assert out.split("\t")[1] == "revoked"

    # Revoking again succeeds and keeps the first revocation.
    status, _, _ = await run(
        sessions, capsys, "revoke-token", "--user-id", str(user_id), "--token-id", token_id
    )
    assert status == 0


async def test_name_is_stored_cleaned_and_only_the_hash_of_the_token(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = await run(sessions, capsys, "create-user", "--name", "  Max\u00a0 Mustermann ")
    user_id = uuid.UUID(out.strip())
    _, token, _ = await run(
        sessions, capsys, "issue-token", "--user-id", str(user_id), "--name", "ci", "--days", "1"
    )

    async with sessions() as session:
        user = await session.get(User, user_id)
        (stored,) = await session.scalars(select(ApiToken).where(ApiToken.owner_id == user_id))
        lifetime = await session.scalar(
            select(ApiToken.expires_at - ApiToken.created_at).where(ApiToken.id == stored.id)
        )
    assert user is not None and user.name == "Max Mustermann"
    assert stored.token_sha256 == token_digest(token.strip())
    assert lifetime is not None and lifetime.days == 1


async def test_disabled_user_tokens_are_refused(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = await run(sessions, capsys, "create-user", "--name", "Max")
    user_id = out.strip()
    _, token, _ = await run(sessions, capsys, "issue-token", "--user-id", user_id, "--name", "x")

    status, _, _ = await run(sessions, capsys, "disable-user", "--user-id", user_id)
    again, _, _ = await run(sessions, capsys, "disable-user", "--user-id", user_id)

    assert (status, again) == (0, 0)
    with pytest.raises(NotAuthenticatedError):
        await authenticate(sessions, token.strip())


async def test_existing_owner_can_get_a_token(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    # The way back to Phase 3-4 data: migration 0004 made DEV_OWNER_ID a user (here: seeded).
    _, token, _ = await run(
        sessions, capsys, "issue-token", "--user-id", str(OWNER_ID), "--name", "dev"
    )

    assert await authenticate(sessions, token.strip()) == OWNER_ID


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("create-user", "--name", "\u200b"), "Error: Name is required."),
        (("create-user", "--name", "Max\x07"), "Error: Name contains control characters."),
        (
            ("issue-token", "--user-id", str(uuid.uuid4()), "--name", "laptop"),
            "Error: User not found.",
        ),
        (
            ("issue-token", "--user-id", str(OWNER_ID), "--name", "laptop", "--days", "0"),
            "Error: Token lifetime must be between 1 and 365 days.",
        ),
        (
            ("issue-token", "--user-id", str(OWNER_ID), "--name", "laptop", "--days", "366"),
            "Error: Token lifetime must be between 1 and 365 days.",
        ),
        (("list-tokens", "--user-id", str(uuid.uuid4())), "Error: User not found."),
        (("disable-user", "--user-id", str(uuid.uuid4())), "Error: User not found."),
        (
            ("revoke-token", "--user-id", str(OWNER_ID), "--token-id", str(uuid.uuid4())),
            "Error: API token not found.",
        ),
    ],
)
async def test_refused_commands_change_nothing(
    sessions: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    message: str,
) -> None:
    users, tokens = await count(sessions, User), await count(sessions, ApiToken)

    status, out, err = await run(sessions, capsys, *argv)

    assert (status, out, err.strip()) == (1, "", message)
    assert (await count(sessions, User), await count(sessions, ApiToken)) == (users, tokens)


async def test_cannot_revoke_another_users_token(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    # revoke-token is scoped by --user-id, like every owner query: a token id alone is not enough.
    _, token, _ = await run(
        sessions, capsys, "issue-token", "--user-id", str(OWNER_ID), "--name", "mine"
    )
    _, listed, _ = await run(sessions, capsys, "list-tokens", "--user-id", str(OWNER_ID))
    token_id = listed.splitlines()[-1].split("\t")[0]
    _, other, _ = await run(sessions, capsys, "create-user", "--name", "Other")

    status, _, err = await run(
        sessions, capsys, "revoke-token", "--user-id", other.strip(), "--token-id", token_id
    )

    assert status == 1
    assert "API token not found." in err
    assert await authenticate(sessions, token.strip()) == OWNER_ID
