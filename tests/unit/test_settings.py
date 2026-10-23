import pytest
from pydantic import ValidationError

from transaction_analytics.domain.csv_parser import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS
from transaction_analytics.settings import Settings

URL = "postgresql+asyncpg://user:S3CRET-PASSWORD@localhost:5432/db"


def test_database_url_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url.get_secret_value() == URL


def test_password_is_masked_in_repr_and_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert "S3CRET-PASSWORD" not in repr(settings)
    assert "S3CRET-PASSWORD" not in str(settings)
    assert "S3CRET-PASSWORD" not in settings.model_dump_json()


def test_missing_database_url_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "database_url" in str(exc.value)


def test_import_limits_default_to_domain_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    for name in ("IMPORT_MAX_BYTES", "IMPORT_MAX_ROWS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.import_max_bytes == DEFAULT_MAX_BYTES
    assert settings.import_max_rows == DEFAULT_MAX_ROWS


def test_import_limits_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("IMPORT_MAX_ROWS", "10")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.import_max_rows == 10


def test_no_setting_chooses_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Phase 5 (ADR 0007): the owner comes from the API token only. A DEV_OWNER_ID left in an old
    # .env is ignored rather than acting as a back door.
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("DEV_OWNER_ID", "00000000-0000-4000-8000-0000000000ff")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert not any("owner" in name for name in Settings.model_fields)
    assert "0000000000ff" not in settings.model_dump_json()


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_import_limits_must_be_positive_integers(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("IMPORT_MAX_BYTES", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_ledger_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.delenv("LEDGER_API_URL", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.ledger_api_url is None
    assert settings.ledger_sync_max_entries == DEFAULT_MAX_ROWS


def test_ledger_url_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("LEDGER_API_URL", "https://ledger.internal:8443/api")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert str(settings.ledger_api_url) == "https://ledger.internal:8443/api"


@pytest.mark.parametrize(
    "value", ["ledger.internal", "file:///etc/passwd", "ftp://ledger.internal", "http://"]
)
def test_ledger_url_must_be_http(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # The only place a ledger call's destination comes from (ADR 0009).
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("LEDGER_API_URL", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "name",
    ["LEDGER_TIMEOUT_SECONDS", "LEDGER_SYNC_TIMEOUT_SECONDS", "LEDGER_SYNC_MAX_ENTRIES"],
)
def test_ledger_limits_must_be_positive(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv(name, "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_operational_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    for name in (
        "DB_STATEMENT_TIMEOUT_SECONDS",
        "DB_CONNECT_TIMEOUT_SECONDS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.db_statement_timeout_seconds == 30
    assert settings.db_connect_timeout_seconds == 5
    assert settings.log_level == "INFO"


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert Settings(_env_file=None).log_level == "DEBUG"  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "LOUD"),
        ("DB_STATEMENT_TIMEOUT_SECONDS", "0"),
        ("DB_STATEMENT_TIMEOUT_SECONDS", "-1"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "0"),
    ],
)
def test_operational_settings_are_checked(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # A statement timeout of 0 would mean "no limit" to PostgreSQL, so it is refused.
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "value",
    [
        "http://svc:S3CRET-LEDGER@ledger:8001",
        "http://svc@ledger:8001",
        "http://:S3CRET-LEDGER@ledger",
    ],
)
def test_ledger_url_must_not_carry_credentials(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # Review fix: httpx2 turns userinfo into Basic auth, which replaces the ledger token's Bearer
    # header on every call. The error must not print the password either.
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("LEDGER_API_URL", value)

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "S3CRET-LEDGER" not in str(exc.value)
