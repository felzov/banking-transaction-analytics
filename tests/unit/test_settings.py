import pytest
from pydantic import ValidationError

from transaction_analytics.domain.csv_parser import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS
from transaction_analytics.settings import DEFAULT_DEV_OWNER_ID, Settings

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


def test_import_limits_and_owner_default_to_domain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    for name in ("DEV_OWNER_ID", "IMPORT_MAX_BYTES", "IMPORT_MAX_ROWS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.dev_owner_id == DEFAULT_DEV_OWNER_ID
    assert settings.import_max_bytes == DEFAULT_MAX_BYTES
    assert settings.import_max_rows == DEFAULT_MAX_ROWS


def test_import_limits_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("IMPORT_MAX_ROWS", "10")
    monkeypatch.setenv("DEV_OWNER_ID", "00000000-0000-4000-8000-0000000000ff")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.import_max_rows == 10
    assert str(settings.dev_owner_id) == "00000000-0000-4000-8000-0000000000ff"


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_import_limits_must_be_positive_integers(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("IMPORT_MAX_BYTES", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
