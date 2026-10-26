from app.core.config import Settings
from app.llm.client import LLMClient
from app.llm.openai_client import OpenAIClient


class LLMNotConfiguredError(RuntimeError):
    """LLM settings are missing or unsupported. The message names settings, never their values."""


def build_llm_client(settings: Settings) -> LLMClient:
    provider = settings.llm_provider.strip().lower()
    if provider != "openai":
        raise LLMNotConfiguredError("LLM_PROVIDER must be set to 'openai'.")
    if not settings.llm_model.strip():
        raise LLMNotConfiguredError("LLM_MODEL is not set.")
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        raise LLMNotConfiguredError("LLM_API_KEY is not set.")
    return OpenAIClient(
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model.strip(),
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
