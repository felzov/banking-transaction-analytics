"""Runtime configuration from environment variables (and a local, git-ignored .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from transaction_analytics.domain.csv_parser import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS


class Settings(BaseSettings):
    # hide_input_in_errors: a rejected value (a URL with a password in it) is not printed.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # SecretStr: the URL contains the password, so repr/str/validation output must mask it.
    database_url: SecretStr

    # The app's connections only (ADR 0010); migrations and the CLI run without these limits.
    db_statement_timeout_seconds: PositiveFloat = 30.0
    db_connect_timeout_seconds: PositiveFloat = 5.0

    # Level of the app's own loggers (ADR 0010); uvicorn's are set with its own options.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    import_max_bytes: PositiveInt = DEFAULT_MAX_BYTES
    import_max_rows: PositiveInt = DEFAULT_MAX_ROWS

    # banking-ledger-api (ADR 0009). Unset: ledger syncs answer 503 ledger_not_configured.
    # http(s) only; the only place a ledger call's destination comes from.
    ledger_api_url: AnyHttpUrl | None = None
    # Per call to the ledger, and for a whole sync (all pages).
    ledger_timeout_seconds: PositiveFloat = 10.0
    ledger_sync_timeout_seconds: PositiveFloat = 120.0
    # Entries one sync may read; a longer history than this cannot be synced (ADR 0009).
    ledger_sync_max_entries: PositiveInt = DEFAULT_MAX_ROWS

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_in_capitals(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("ledger_api_url")
    @classmethod
    def _no_credentials_in_ledger_url(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        # httpx2 turns user:password@ into Basic auth, which would replace the ledger token's
        # Bearer header on every call. Ledger calls authenticate with the caller's token only.
        if url is not None and (url.username or url.password):
            raise ValueError("LEDGER_API_URL must not contain a user name or password.")
        return url


@lru_cache
def get_settings() -> Settings:
    """Read settings on first use, not at import time, so unit tests need no environment."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
