"""Operator commands for users and API tokens (ADR 0007).

    uv run python -m transaction_analytics.cli create-user --name "Max"
    uv run python -m transaction_analytics.cli issue-token --user-id <id> --name laptop [--days 90]
    uv run python -m transaction_analytics.cli list-tokens --user-id <id>
    uv run python -m transaction_analytics.cli revoke-token --user-id <id> --token-id <id>
    uv run python -m transaction_analytics.cli disable-user --user-id <id>

Thin, like the API routes: services do the work. What a script needs (a new id, the token) goes to
stdout; everything else to stderr. issue-token prints the token once; it is never stored. A database
error prints only its class name (ADR 0003, item 10).
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_analytics.data.db import create_engine, create_sessionmaker
from transaction_analytics.data.errors import error_kind
from transaction_analytics.data.models import ApiToken
from transaction_analytics.domain.auth import DEFAULT_TOKEN_DAYS, MAX_TOKEN_DAYS
from transaction_analytics.domain.errors import LabelValidationError, TokenValidationError
from transaction_analytics.services import auth as auth_service
from transaction_analytics.services.errors import ServiceError
from transaction_analytics.settings import get_settings

Command = Callable[[AsyncSession, argparse.Namespace], Awaitable[None]]


def _note(message: str) -> None:
    print(message, file=sys.stderr)


async def _create_user(session: AsyncSession, args: argparse.Namespace) -> None:
    user = await auth_service.create_user(session, name=args.name)
    print(user.id)


async def _disable_user(session: AsyncSession, args: argparse.Namespace) -> None:
    await auth_service.disable_user(session, owner_id=args.user_id)
    _note("User disabled; their tokens are refused from now on.")


async def _issue_token(session: AsyncSession, args: argparse.Namespace) -> None:
    issued = await auth_service.issue_token(
        session, owner_id=args.user_id, name=args.name, days=args.days
    )
    _note(f"Token {issued.token.id}, expires {issued.token.expires_at.isoformat()}.")
    _note("It is shown only now and cannot be recovered; store it in a password manager.")
    print(issued.plaintext)


def _status(token: ApiToken, now: datetime) -> str:
    # Display only, so the local clock is fine; authentication compares with the database clock.
    if token.revoked_at is not None:
        return "revoked"
    return "expired" if token.expires_at <= now else "active"


async def _list_tokens(session: AsyncSession, args: argparse.Namespace) -> None:
    now = datetime.now(UTC)
    for token in await auth_service.list_tokens(session, owner_id=args.user_id):
        expires = token.expires_at.isoformat()
        print(f"{token.id}\t{_status(token, now)}\texpires {expires}\t{token.name}")


async def _revoke_token(session: AsyncSession, args: argparse.Namespace) -> None:
    await auth_service.revoke_token(session, owner_id=args.user_id, token_id=args.token_id)
    _note("Token revoked.")


COMMANDS: dict[str, Command] = {
    "create-user": _create_user,
    "disable-user": _disable_user,
    "issue-token": _issue_token,
    "list-tokens": _list_tokens,
    "revoke-token": _revoke_token,
}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m transaction_analytics.cli", description="Manage users and API tokens."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user", help="create a user and print its id")
    create.add_argument("--name", required=True)

    disable = commands.add_parser("disable-user", help="refuse all of a user's tokens")
    disable.add_argument("--user-id", type=uuid.UUID, required=True)

    issue = commands.add_parser("issue-token", help="issue an API token and print it once")
    issue.add_argument("--user-id", type=uuid.UUID, required=True)
    issue.add_argument("--name", required=True, help="what the token is for, e.g. laptop")
    issue.add_argument(
        "--days",
        type=int,
        default=DEFAULT_TOKEN_DAYS,
        help=f"lifetime in days, 1-{MAX_TOKEN_DAYS} (default {DEFAULT_TOKEN_DAYS})",
    )

    list_ = commands.add_parser("list-tokens", help="list a user's tokens (never their text)")
    list_.add_argument("--user-id", type=uuid.UUID, required=True)

    revoke = commands.add_parser("revoke-token", help="revoke one of a user's tokens")
    revoke.add_argument("--user-id", type=uuid.UUID, required=True)
    revoke.add_argument("--token-id", type=uuid.UUID, required=True)

    return parser.parse_args(argv)


async def run(args: argparse.Namespace, sessions: async_sessionmaker[AsyncSession]) -> int:
    """Exit status: 0 on success, 1 when the command is refused or the database fails."""
    try:
        async with sessions() as session:
            await COMMANDS[args.command](session, args)
    except (LabelValidationError, TokenValidationError, ServiceError) as exc:
        _note(f"Error: {exc.message}")
        return 1
    except DBAPIError as exc:
        _note(f"Error: the database refused the command ({error_kind(exc)}).")
        return 1
    return 0


async def _run_with_settings(args: argparse.Namespace) -> int:
    engine = create_engine(get_settings().database_url.get_secret_value())
    try:
        return await run(args, create_sessionmaker(engine))
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    # Arguments first: --help and usage errors need no DATABASE_URL.
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_run_with_settings(args))


if __name__ == "__main__":
    raise SystemExit(main())
