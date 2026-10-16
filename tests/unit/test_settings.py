import pytest
from pydantic import ValidationError

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
