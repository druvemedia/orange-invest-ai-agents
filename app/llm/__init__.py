"""Provider-neutral LLM access for the agents.

Agents depend only on ``LLMClient``. ``OpenAIClient`` is the production
implementation; ``ScriptedLLMClient`` returns canned responses for tests.
"""

from app.llm.client import LLMClient, LLMError, LLMErrorKind, LLMRequest, validate_output
from app.llm.fake import ScriptedLLMClient
from app.llm.factory import BilledCallsNotAllowedError, LLMNotConfiguredError, build_llm_client, check_llm_settings
from app.llm.openai_client import OpenAIClient

__all__ = [
    "BilledCallsNotAllowedError",
    "LLMClient",
    "LLMError",
    "LLMErrorKind",
    "LLMNotConfiguredError",
    "LLMRequest",
    "OpenAIClient",
    "ScriptedLLMClient",
    "build_llm_client",
    "check_llm_settings",
    "validate_output",
]
