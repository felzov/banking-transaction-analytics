"""Runtime configuration from environment variables (and a local, git-ignored .env file)."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SecretStr: the URL contains the password, so repr/str/validation output must mask it.
    database_url: SecretStr


@lru_cache
def get_settings() -> Settings:
    """Read settings on first use, not at import time, so unit tests need no environment."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
