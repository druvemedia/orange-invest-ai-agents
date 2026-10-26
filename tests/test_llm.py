"""LLM client layer: OpenAI error mapping, output validation, configuration and redaction.

The OpenAI SDK client is replaced by a stub, so nothing here touches the network.
"""

import asyncio
import logging

import httpx2
import openai
import pytest

from app.core.config import Settings
from app.core.redaction import redact
from app.llm import (
    LLMError,
    LLMErrorKind,
    LLMNotConfiguredError,
    LLMRequest,
    OpenAIClient,
    ScriptedLLMClient,
    build_llm_client,
)
from app.schemas.common import StrictModel

FAKE_KEY = "sk-proj-FAKEKEYFORTESTS0123456789abcdef"
REQUEST = LLMRequest(agent="unit", instructions="Do the thing.", input_json='{"secret_input": "customer data"}')
URL = "https://api.openai.com/v1/responses"


class Answer(StrictModel):
    value: int


class StubResponses:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class StubSDK:
    def __init__(self, outcome):
        self.responses = StubResponses(outcome)


class Parsed:
    def __init__(self, text):
        self.output_text = text


def client(outcome) -> OpenAIClient:
    return OpenAIClient(api_key=FAKE_KEY, model="test-model", timeout_seconds=5, max_output_tokens=500, sdk_client=StubSDK(outcome))


def status_error(cls, status: int):
    request = httpx2.Request("POST", URL)
    body = {"error": {"message": f"Incorrect API key provided: {FAKE_KEY}"}}
    response = httpx2.Response(status, request=request, json=body)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


# --- OpenAI client ----------------------------------------------------------------------------------


def test_success_validates_output_and_passes_structured_format():
    c = client(Parsed('{"value": 3}'))

    out = asyncio.run(c.generate(REQUEST, Answer))

    assert out == Answer(value=3)
    kwargs = c._client.responses.kwargs
    assert kwargs["text_format"] is Answer
    assert kwargs["model"] == "test-model"
    assert kwargs["max_output_tokens"] == 500
    assert kwargs["instructions"] == REQUEST.instructions


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (lambda: openai.APITimeoutError(request=httpx2.Request("POST", URL)), LLMErrorKind.TIMEOUT, True),
        (lambda: openai.APIConnectionError(request=httpx2.Request("POST", URL)), LLMErrorKind.UNAVAILABLE, True),
        (lambda: status_error(openai.RateLimitError, 429), LLMErrorKind.RATE_LIMITED, True),
        (lambda: status_error(openai.InternalServerError, 503), LLMErrorKind.UNAVAILABLE, True),
        (lambda: status_error(openai.AuthenticationError, 401), LLMErrorKind.REJECTED, False),
        (lambda: status_error(openai.BadRequestError, 400), LLMErrorKind.REJECTED, False),
    ],
)
def test_provider_errors_are_classified_without_leaking_details(exc, kind, retryable):
    with pytest.raises(LLMError) as info:
        asyncio.run(client(exc()).generate(REQUEST, Answer))

    error = info.value
    assert error.kind is kind
    assert error.retryable is retryable
    assert FAKE_KEY not in error.message and "sk-" not in error.message
    assert "customer data" not in error.message
    assert error.__cause__ is None  # the SDK exception (and its body) is not chained


@pytest.mark.parametrize("text", [None, "", "not json", '{"value": "three"}', '{"value": 1, "extra": 2}'])
def test_unusable_output_is_invalid_output(text):
    with pytest.raises(LLMError) as info:
        asyncio.run(client(Parsed(text)).generate(REQUEST, Answer))

    assert info.value.kind is LLMErrorKind.INVALID_OUTPUT
    assert info.value.retryable is True
    assert "three" not in info.value.message  # the output itself is not echoed


def test_sdk_retries_are_disabled_so_the_engine_bounds_them():
    real = OpenAIClient(api_key=FAKE_KEY, model="m", timeout_seconds=5, max_output_tokens=500)

    assert real._client.max_retries == 0  # constructing the SDK client makes no request


def test_llm_logging_contains_no_key_or_content(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.llm"):
        with pytest.raises(LLMError):
            asyncio.run(client(status_error(openai.AuthenticationError, 401)).generate(REQUEST, Answer))
        asyncio.run(client(Parsed('{"value": 1}')).generate(REQUEST, Answer))

    assert FAKE_KEY not in caplog.text
    assert "customer data" not in caplog.text
    assert "agent=unit" in caplog.text


# --- Scripted client ------------------------------------------------------------------------------------


def test_scripted_client_validates_like_the_real_one():
    llm = ScriptedLLMClient({"unit": [{"value": "nope"}, {"value": 2}]})

    with pytest.raises(LLMError) as info:
        asyncio.run(llm.generate(REQUEST, Answer))
    assert info.value.kind is LLMErrorKind.INVALID_OUTPUT
    assert asyncio.run(llm.generate(REQUEST, Answer)) == Answer(value=2)
    assert asyncio.run(llm.generate(REQUEST, Answer)) == Answer(value=2)  # last step repeats
    assert len(llm.calls_for("unit")) == 3


# --- Configuration ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "missing"),
    [
        ({}, "LLM_PROVIDER"),
        ({"llm_provider": "other"}, "LLM_PROVIDER"),
        ({"llm_provider": "openai"}, "LLM_MODEL"),
        ({"llm_provider": "openai", "llm_model": "m"}, "LLM_API_KEY"),
        ({"llm_provider": "openai", "llm_model": "m", "llm_api_key": "  "}, "LLM_API_KEY"),
    ],
)
def test_missing_configuration_is_reported_by_name(clean_env, fields, missing):
    with pytest.raises(LLMNotConfiguredError, match=missing):
        build_llm_client(Settings(_env_file=None, **fields))


def test_configured_settings_build_openai_client_without_exposing_key(clean_env):
    settings = Settings(_env_file=None, llm_provider="OpenAI", llm_model="m", llm_api_key=FAKE_KEY)

    built = build_llm_client(settings)

    assert isinstance(built, OpenAIClient)
    assert FAKE_KEY not in repr(settings)


# --- Redaction ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"Incorrect API key provided: {FAKE_KEY}",
        "Incorrect API key provided: sk-proj-****************************cdef",
        "Authorization: Bearer abcdef1234567890",
        'api_key="supersecretvalue"',
        "password=hunter2hunter2",
    ],
)
def test_redact_removes_credentials(text):
    out = redact(text)

    assert "[REDACTED]" in out
    for secret in (FAKE_KEY, "abcdef1234567890", "supersecretvalue", "hunter2hunter2", "sk-proj-***"):
        assert secret not in out


def test_redact_removes_explicit_secrets_and_keeps_ordinary_text():
    assert redact("token budget of 4000 tokens used") == "token budget of 4000 tokens used"
    assert redact("value myvalue123 here", secrets=["myvalue123"]) == "value [REDACTED] here"


# --- SDK-side parsing and usage (Phase 3.1) ------------------------------------------------------------


class StubUsage:
    input_tokens = 1200
    output_tokens = 340


class ParsedWithUsage(Parsed):
    usage = StubUsage()


def sdk_validation_error():
    # What responses.parse raises when the SDK's own model_validate_json rejects the output.
    try:
        Answer.model_validate_json('{"value": "not-an-int-but-leaked-output"}')
    except Exception as exc:  # pydantic.ValidationError
        return exc
    raise AssertionError("expected a validation error")


def test_sdk_side_validation_error_is_retryable_invalid_output():
    c = client(sdk_validation_error())

    with pytest.raises(LLMError) as info:
        asyncio.run(c.generate(REQUEST, Answer))

    assert info.value.kind is LLMErrorKind.INVALID_OUTPUT
    assert info.value.retryable is True
    assert "leaked-output" not in info.value.message
    assert c.calls[-1].outcome == "invalid_output"


def test_call_stats_record_latency_and_token_usage_without_content():
    c = client(ParsedWithUsage('{"value": 7}'))

    asyncio.run(c.generate(REQUEST, Answer))

    [stats] = c.calls
    assert (stats.agent, stats.outcome, stats.input_tokens, stats.output_tokens) == ("unit", "ok", 1200, 340)
    assert stats.latency_ms >= 0
    assert "customer data" not in repr(stats)


def test_failed_calls_are_recorded_without_tokens():
    c = client(status_error(openai.RateLimitError, 429))

    with pytest.raises(LLMError):
        asyncio.run(c.generate(REQUEST, Answer))

    assert [(s.outcome, s.input_tokens) for s in c.calls] == [("rate_limited", None)]
