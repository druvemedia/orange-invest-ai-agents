"""Per-request input-token budget: an oversized request is refused before anything is sent to OpenAI.

The bound needs no tokenizer. gpt-4o-mini's tokenizer is byte-level, so a token covers at least one UTF-8 byte and
our own text (instructions and input) can never be more tokens than bytes. The output schema is rendered by the
provider in its own format, so it counts twice, and a fixed allowance covers message framing.
"""

import asyncio
import json

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from app.agents import AgentConfig, OrchestratorAgent
from app.core.config import Settings
from app.evaluation.dataset import load_cases
from app.llm import LLMError, LLMErrorKind, LLMRequest, OpenAIClient, ScriptedLLMClient
from app.schemas.common import StrictModel
from tests.test_llm import Answer, Parsed

FAKE_KEY = "sk-proj-BUDGETTESTNOTREAL0123456789"
EVAL_01 = {c.case_id: c for c in load_cases()}["EVAL-01"]


class CountingResponses:
    """Records every request that would have reached OpenAI."""

    def __init__(self):
        self.sent = []

    async def parse(self, **kwargs):
        self.sent.append(kwargs)
        return Parsed('{"value": 1}')


class CountingSDK:
    def __init__(self):
        self.responses = CountingResponses()


def client(budget, sdk=None):
    return OpenAIClient(api_key=FAKE_KEY, model="m", timeout_seconds=5, max_output_tokens=500, max_input_tokens=budget,
                        sdk_client=sdk or CountingSDK())


def request(input_json='{"x": 1}', instructions="Answer."):
    return LLMRequest(agent="reasoning", instructions=instructions, input_json=input_json)


def test_an_oversized_request_is_refused_before_anything_is_sent():
    sdk = CountingSDK()

    with pytest.raises(LLMError) as info:
        asyncio.run(client(2_000, sdk).generate(request(json.dumps({"x": "a" * 5_000})), Answer))

    assert sdk.responses.sent == []
    assert info.value.kind is LLMErrorKind.REJECTED and not info.value.retryable
    assert "not sent" in str(info.value) and "LLM_MAX_INPUT_TOKENS" in str(info.value)
    assert FAKE_KEY not in str(info.value)


def test_a_request_within_the_budget_is_sent_once():
    sdk = CountingSDK()

    assert asyncio.run(client(64_000, sdk).generate(request(), Answer)).value == 1
    assert len(sdk.responses.sent) == 1 and sdk.responses.sent[0]["max_output_tokens"] == 500


def test_the_bound_counts_bytes_of_every_part():
    from app.llm.openai_client import FRAMING_TOKENS, input_token_bound

    base = input_token_bound(request("{}", "a"), Answer)
    assert input_token_bound(request("{}", "a" + "é" * 100), Answer) - base == 200  # bytes, not characters
    assert input_token_bound(request('{"é": "' + "語" * 10 + '"}', "a"), Answer) - base >= 30 + 2

    class Bigger(StrictModel):
        value: int
        explanation: str
        evidence_refs: list[str]

    def schema_bytes(model):
        return len(json.dumps(to_strict_json_schema(model), separators=(",", ":")).encode())

    grown = input_token_bound(request("{}", "a"), Bigger) - base
    assert grown == 2 * (schema_bytes(Bigger) - schema_bytes(Answer))  # the schema counts twice
    assert base >= FRAMING_TOKENS + 2 * schema_bytes(Answer) + 3


def test_budget_setting_defaults_overrides_and_bounds(clean_env, monkeypatch):
    assert Settings(_env_file=None).llm_max_input_tokens == 64_000

    monkeypatch.setenv("LLM_MAX_INPUT_TOKENS", "32000")
    assert Settings(_env_file=None).llm_max_input_tokens == 32_000

    for bad in ("500", "200000"):
        monkeypatch.setenv("LLM_MAX_INPUT_TOKENS", bad)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_the_factory_passes_the_budget_to_the_client(clean_env, monkeypatch):
    from app.llm import build_llm_client

    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kwargs: object())
    settings = Settings(_env_file=None, llm_provider="openai", llm_model="m", llm_api_key=FAKE_KEY,
                        llm_allow_billed_calls=True, llm_max_input_tokens=12_345)

    assert build_llm_client(settings)._max_input_tokens == 12_345


def test_the_default_budget_leaves_headroom_over_realistic_requests():
    """A replay of live run 1's model outputs on EVAL-01: the largest request must be well inside the default."""
    from app.llm.openai_client import input_token_bound
    from tests.test_phase6_quality import live_like_script

    bounds = []

    class Recording(ScriptedLLMClient):
        async def generate(self, req, output_type):
            bounds.append(input_token_bound(req, output_type))
            return await super().generate(req, output_type)

    asyncio.run(OrchestratorAgent(Recording(live_like_script()), EVAL_01.tools(), AgentConfig())
                .investigate(EVAL_01.incident, "inv-budget"))

    assert 1.5 * max(bounds) <= Settings(_env_file=None).llm_max_input_tokens, max(bounds)


def test_a_budget_too_small_for_any_request_sends_nothing_and_the_investigation_still_ends():
    sdk = CountingSDK()
    llm = client(1_000, sdk)  # below the framing allowance alone, so every request is refused

    result = asyncio.run(OrchestratorAgent(llm, EVAL_01.tools(), AgentConfig()).investigate(EVAL_01.incident, "inv-tiny"))

    assert sdk.responses.sent == []
    assert result.report.generated_by in ("fallback", "rules")  # never presented as model-written
    assert result.report.report.conclusion == "inconclusive"
    agent_attempts = [len(t.attempts) for i, t in result.investigation.tasks.items() if not i.startswith("ev.")]
    assert agent_attempts and max(agent_attempts) == 1  # refused requests are not retried
