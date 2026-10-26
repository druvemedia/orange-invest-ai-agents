"""Provider-neutral LLM access for the agents.

Agents depend only on ``LLMClient``. ``OpenAIClient`` is the production
implementation; ``ScriptedLLMClient`` returns canned responses for tests.
"""

from app.llm.client import LLMClient, LLMError, LLMErrorKind, LLMRequest, validate_output
from app.llm.fake import ScriptedLLMClient
from app.llm.factory import LLMNotConfiguredError, build_llm_client
from app.llm.openai_client import OpenAIClient

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMErrorKind",
    "LLMNotConfiguredError",
    "LLMRequest",
    "OpenAIClient",
    "ScriptedLLMClient",
    "build_llm_client",
    "validate_output",
]
