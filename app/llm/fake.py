"""Deterministic LLM double for tests and offline demos. It never touches the network."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.llm.client import LLMError, LLMRequest, OutputT, validate_output

# A scripted step: a response (dict, JSON string or model), an LLMError to raise,
# or a callable that receives the request and returns either of those.
Step = Any


@dataclass
class RecordedCall:
    agent: str
    input_json: str
    output_type: str


class ScriptedLLMClient:
    """Answers each agent's calls from its own script, in order. The last step repeats.

    Responses go through the same schema validation as the real client, so a malformed
    scripted response produces ``LLMError(INVALID_OUTPUT)`` exactly as live output would.
    """

    def __init__(self, scripts: Mapping[str, Sequence[Step] | Step]) -> None:
        self._scripts = {
            agent: list(steps) if isinstance(steps, (list, tuple)) else [steps] for agent, steps in scripts.items()
        }
        self._positions: dict[str, int] = {}
        self.calls: list[RecordedCall] = []

    async def generate(self, request: LLMRequest, output_type: type[OutputT]) -> OutputT:
        self.calls.append(RecordedCall(request.agent, request.input_json, output_type.__name__))
        steps = self._scripts.get(request.agent)
        if not steps:
            raise AssertionError(f"no scripted response for agent '{request.agent}'")
        position = self._positions.get(request.agent, 0)
        self._positions[request.agent] = position + 1
        step = steps[min(position, len(steps) - 1)]
        if callable(step):
            step = step(request)
        if isinstance(step, LLMError):
            raise step
        return validate_output(step, output_type)

    def calls_for(self, agent: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.agent == agent]
