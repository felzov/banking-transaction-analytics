"""CLI argument handling, which needs no database. Commands are tested in tests/integration."""

import uuid

import pytest

from transaction_analytics import cli
from transaction_analytics.domain.auth import DEFAULT_TOKEN_DAYS

USER_ID = "00000000-0000-4000-8000-000000000001"


def test_issue_token_defaults_to_the_standard_lifetime() -> None:
    args = cli.parse_args(["issue-token", "--user-id", USER_ID, "--name", "laptop"])

    assert args.user_id == uuid.UUID(USER_ID)
    assert args.days == DEFAULT_TOKEN_DAYS


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["unknown-command"],
        ["create-user"],
        ["issue-token", "--name", "laptop"],
        ["issue-token", "--user-id", "not-a-uuid", "--name", "laptop"],
        ["issue-token", "--user-id", USER_ID, "--name", "laptop", "--days", "many"],
        ["revoke-token", "--user-id", USER_ID],
    ],
)
def test_usage_errors_exit_before_any_database_use(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_settings() -> None:
        raise AssertionError("settings read")

    monkeypatch.setattr(cli, "get_settings", no_settings)

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 2


REQUIRED_ARGS = {
    "create-user": ["--name", "Max"],
    "disable-user": ["--user-id", USER_ID],
    "issue-token": ["--user-id", USER_ID, "--name", "laptop"],
    "list-tokens": ["--user-id", USER_ID],
    "revoke-token": ["--user-id", USER_ID, "--token-id", USER_ID],
}


def test_parser_commands_and_handlers_match() -> None:
    assert set(REQUIRED_ARGS) == set(cli.COMMANDS)
    for command, args in REQUIRED_ARGS.items():
        assert cli.parse_args([command, *args]).command == command
