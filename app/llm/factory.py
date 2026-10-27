from app.core.config import Settings
from app.llm.client import LLMClient
from app.llm.openai_client import OpenAIClient


class LLMNotConfiguredError(RuntimeError):
    """LLM settings are missing or unsupported. The message names settings, never their values."""


class BilledCallsNotAllowedError(LLMNotConfiguredError):
    """A billed provider is configured, but billed calls were not explicitly enabled."""


def check_llm_settings(settings: Settings, *, billing_confirmed: bool = False) -> None:
    """Raise ``LLMNotConfiguredError`` unless a real client may be built. Builds nothing and makes no request.

    Real providers are billed, so they also need an explicit opt-in: ``LLM_ALLOW_BILLED_CALLS=true``, or
    ``billing_confirmed`` from a command whose user already confirmed the cost (``--confirm-billed``).
    """
    provider = settings.llm_provider.strip().lower()
    if provider != "openai":
        raise LLMNotConfiguredError("LLM_PROVIDER must be set to 'openai'.")
    if not settings.llm_model.strip():
        raise LLMNotConfiguredError("LLM_MODEL is not set.")
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        raise LLMNotConfiguredError("LLM_API_KEY is not set.")
    if not (settings.llm_allow_billed_calls or billing_confirmed):
        raise BilledCallsNotAllowedError(
            "Billed model calls are not enabled: set LLM_ALLOW_BILLED_CALLS=true to allow real, billed calls "
            "to the configured model."
        )


def build_llm_client(settings: Settings, *, billing_confirmed: bool = False) -> LLMClient:
    check_llm_settings(settings, billing_confirmed=billing_confirmed)
    return OpenAIClient(
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model.strip(),
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
