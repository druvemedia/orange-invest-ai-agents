"""Application settings loaded from environment variables and an optional .env file.

Secrets are typed as ``SecretStr`` so they are masked in ``repr()``/logs and are
only revealed via an explicit ``.get_secret_value()`` call on the server side.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Orange Invest AI Agent"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # LLM configuration (not used until the agent phases). The key is optional until then.
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
