"""The LLM interface the agents use, and its error type.

``LLMError.message`` is always written by this codebase from fixed templates. It
never contains provider response bodies, request content or credentials.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.tools.datastore import summarize_validation_error

OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class LLMRequest:
    agent: str
    instructions: str
    # JSON document with the agent's typed input. Evidence inside it is data, not instructions.
    input_json: str


class LLMErrorKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"  # connection failures and 5xx responses
    REJECTED = "rejected"  # 4xx other than rate limiting: auth, permissions, bad request
    INVALID_OUTPUT = "invalid_output"  # unparseable, refused, truncated or schema-invalid output


RETRYABLE_KINDS = frozenset({LLMErrorKind.TIMEOUT, LLMErrorKind.RATE_LIMITED, LLMErrorKind.UNAVAILABLE, LLMErrorKind.INVALID_OUTPUT})


class LLMError(Exception):
    def __init__(self, kind: LLMErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS


class LLMClient(Protocol):
    async def generate(self, request: LLMRequest, output_type: type[OutputT]) -> OutputT:
        """Return output validated against ``output_type`` or raise ``LLMError``."""
        ...


def validate_output(raw: str | dict | BaseModel, output_type: type[OutputT]) -> OutputT:
    """Validate model output against the schema. Raises LLMError(INVALID_OUTPUT) without echoing the output."""
    try:
        if isinstance(raw, str):
            return output_type.model_validate_json(raw)
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(mode="json")
        return output_type.model_validate(raw)
    except ValidationError as exc:
        raise LLMError(
            LLMErrorKind.INVALID_OUTPUT,
            f"Model output did not match {output_type.__name__}: {summarize_validation_error(exc)}",
        ) from None
