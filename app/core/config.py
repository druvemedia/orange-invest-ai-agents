"""Application settings loaded from environment variables and an optional .env file.

Secrets are typed as ``SecretStr`` so they are masked in ``repr()``/logs and are
only revealed via an explicit ``.get_secret_value()`` call on the server side.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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

    # LLM configuration. Optional: the API and tests run without it; agents need it (see app/llm).
    llm_provider: str = ""  # "openai" is the only supported provider
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    llm_max_output_tokens: int = Field(default=4000, ge=256, le=32000)

    # Investigation limits used by the agents' orchestrator.
    agent_max_critic_rounds: int = Field(default=2, ge=1, le=5)
    agent_max_evidence_requests: int = Field(default=3, ge=0, le=5)
    investigation_deadline_seconds: float = Field(default=300.0, gt=0, le=3600)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
