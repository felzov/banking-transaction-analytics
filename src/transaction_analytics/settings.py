"""Runtime configuration from environment variables (and a local, git-ignored .env file)."""

import uuid
from functools import lru_cache

from pydantic import PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from transaction_analytics.domain.csv_parser import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS

# Not a secret: a fixed placeholder identity until authentication exists (Phase 5, ADR 0005).
DEFAULT_DEV_OWNER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SecretStr: the URL contains the password, so repr/str/validation output must mask it.
    database_url: SecretStr

    # Every request acts as this owner until Phase 5 replaces api.deps.get_owner_id.
    dev_owner_id: uuid.UUID = DEFAULT_DEV_OWNER_ID

    import_max_bytes: PositiveInt = DEFAULT_MAX_BYTES
    import_max_rows: PositiveInt = DEFAULT_MAX_ROWS


@lru_cache
def get_settings() -> Settings:
    """Read settings on first use, not at import time, so unit tests need no environment."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
