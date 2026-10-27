"""OpenAI implementation of LLMClient using the Responses API with structured outputs."""

import json
import logging
import time
from dataclasses import dataclass
from functools import cache
from typing import Any

import openai
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from app.llm.client import LLMError, LLMErrorKind, LLMRequest, OutputT, validate_output
from app.tools.datastore import summarize_validation_error

logger = logging.getLogger(__name__)

# Input-token bound without a tokenizer. The tokenizer is byte-level, so a token covers at least one UTF-8 byte:
# our instructions and input can never be more tokens than bytes. The output schema is rendered by the provider
# in its own format, so it counts twice, and this allowance covers the provider's message framing. The two
# allowances are estimates; each call logs its bound next to the reported input tokens so they can be checked.
FRAMING_TOKENS = 1_000


@cache
def _schema_bytes(output_type: type) -> int:
    return len(json.dumps(to_strict_json_schema(output_type), separators=(",", ":")).encode())


def input_token_bound(request: LLMRequest, output_type: type) -> int:
    """An upper bound on a request's input tokens: instructions, input, output schema and framing."""
    return (len(request.instructions.encode()) + len(request.input_json.encode()) + 2 * _schema_bytes(output_type)
            + FRAMING_TOKENS)


@dataclass(frozen=True)
class CallStats:
    """One model call: no content, only what is needed to report cost and reliability."""

    agent: str
    outcome: str  # "ok" or an LLMErrorKind value
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None


class OpenAIClient:
    """Calls ``responses.parse`` with the output model as ``text_format``.

    SDK retries are disabled (``max_retries=0``) so that the orchestration engine is the
    single, bounded place where retries happen. Provider exceptions are mapped to
    ``LLMError`` with fixed messages; their own text is never stored or logged.
    ``calls`` records outcome, latency and token usage per call. A request whose input bound exceeds
    ``max_input_tokens`` is refused before it is sent (non-retryable; nothing is recorded as a call).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        max_input_tokens: int = 64_000,
        sdk_client: Any | None = None,
    ) -> None:
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_input_tokens = max_input_tokens
        self._client = sdk_client or openai.AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self.calls: list[CallStats] = []

    async def generate(self, request: LLMRequest, output_type: type[OutputT]) -> OutputT:
        bound = input_token_bound(request, output_type)
        if bound > self._max_input_tokens:
            logger.warning("llm agent=%s not sent: input bound %d > budget %d", request.agent, bound, self._max_input_tokens)
            raise LLMError(LLMErrorKind.REJECTED, f"Request not sent: its input could reach {bound} tokens, over the "
                                                  f"budget of {self._max_input_tokens} (LLM_MAX_INPUT_TOKENS).")
        started = time.perf_counter()
        response = None
        try:
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    instructions=request.instructions,
                    input=request.input_json,
                    text_format=output_type,
                    max_output_tokens=self._max_output_tokens,
                )
            except openai.APIError as exc:
                raise _map_error(exc) from None
            except openai.OpenAIError as exc:  # e.g. LengthFinishReasonError, ContentFilterFinishReasonError
                raise LLMError(LLMErrorKind.INVALID_OUTPUT, f"Model output was not usable ({type(exc).__name__}).") from None
            except ValidationError as exc:
                # The SDK parses the output into text_format itself; a schema-valid response can still
                # fail our model validators (e.g. a timestamp without a timezone).
                raise LLMError(
                    LLMErrorKind.INVALID_OUTPUT,
                    f"Model output did not match {output_type.__name__}: {summarize_validation_error(exc)}",
                ) from None

            text = getattr(response, "output_text", None)
            if not text:
                raise LLMError(LLMErrorKind.INVALID_OUTPUT, "Model returned no structured output (possibly a refusal).")
            # Validate again ourselves so every model-level validator runs, whatever the SDK did.
            result = validate_output(text, output_type)
        except LLMError as error:
            self._record(request.agent, error.kind.value, started, response)
            logger.warning("llm agent=%s error=%s", request.agent, error.kind)
            raise
        stats = self._record(request.agent, "ok", started, response)
        logger.info(
            "llm agent=%s ok in %.0fms tokens in=%s (bound %d) out=%s",
            request.agent, stats.latency_ms, stats.input_tokens, bound, stats.output_tokens,
        )
        return result

    def _record(self, agent: str, outcome: str, started: float, response: Any) -> CallStats:
        usage = getattr(response, "usage", None)
        stats = CallStats(
            agent=agent,
            outcome=outcome,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        self.calls.append(stats)
        return stats


def _map_error(exc: openai.APIError) -> LLMError:
    if isinstance(exc, openai.APITimeoutError):
        return LLMError(LLMErrorKind.TIMEOUT, "The model request timed out.")
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(LLMErrorKind.UNAVAILABLE, "Could not connect to the model provider.")
    if isinstance(exc, openai.RateLimitError):
        return LLMError(LLMErrorKind.RATE_LIMITED, "The model provider rate-limited the request (HTTP 429).")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status >= 500:
            return LLMError(LLMErrorKind.UNAVAILABLE, f"The model provider returned a server error (HTTP {status}).")
        if status in (401, 403):
            return LLMError(LLMErrorKind.REJECTED, f"The model provider rejected the credentials (HTTP {status}).")
        return LLMError(LLMErrorKind.REJECTED, f"The model provider rejected the request (HTTP {status}).")
    if isinstance(exc, openai.APIResponseValidationError):
        return LLMError(LLMErrorKind.INVALID_OUTPUT, "The model provider returned an unexpected response.")
    return LLMError(LLMErrorKind.UNAVAILABLE, f"Model provider error ({type(exc).__name__}).")
