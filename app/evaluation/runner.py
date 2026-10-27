"""Runs both approaches on the same cases, then scores them.

Investigation runs see only ``EvalCase.incident`` and ``EvalCase.tools()``. Gold labels are loaded
after every run has finished, and only for scoring.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.agents import AgentConfig, OrchestratorAgent
from app.baseline import BaselineConfig, SingleAgentInvestigator
from app.core.redaction import redact
from app.evaluation.dataset import EVAL_DIR, EvalCase, load_cases, load_labels
from app.evaluation.metrics import aggregate, score
from app.evaluation.normalize import Approach, RunRecord, failed_run, from_multi_agent, from_single_agent
from app.llm import LLMClient, LLMRequest

APPROACHES: tuple[Approach, ...] = ("multi_agent", "single_agent")


class MeteredLLM:
    """Counts model calls and keeps request inputs in memory (used by the label-leak test)."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.call_count = 0
        self.inputs: list[str] = []

    async def generate(self, request: LLMRequest, output_type):
        self.call_count += 1
        self.inputs.append(request.input_json)
        return await self.inner.generate(request, output_type)

    def tokens(self) -> tuple[int | None, int | None]:
        stats = [c for c in getattr(self.inner, "calls", []) if getattr(c, "input_tokens", None) is not None]
        if not stats:
            return None, None
        return sum(c.input_tokens for c in stats), sum(c.output_tokens or 0 for c in stats)


async def run_case(
    case: EvalCase,
    approach: Approach,
    make_llm: Callable[[], LLMClient],
    mode: str,
    agent_config: AgentConfig,
    baseline_config: BaselineConfig,
) -> tuple[RunRecord, MeteredLLM]:
    llm = MeteredLLM(make_llm())  # a fresh client per run, so usage is per run
    tools = case.tools()
    started = time.perf_counter()
    try:
        if approach == "multi_agent":
            result = await OrchestratorAgent(llm, tools, agent_config).investigate(case.incident, f"{case.case_id}-multi")
            convert = from_multi_agent
        else:
            result = await SingleAgentInvestigator(llm, tools, baseline_config).investigate(case.incident, f"{case.case_id}-single")
            convert = from_single_agent
        record = convert(case.case_id, mode, result, latency=round(time.perf_counter() - started, 3),
                         model_calls=llm.call_count, tokens=llm.tokens())
    except Exception as exc:  # a crashed run is recorded and scored as failed, never dropped
        record = failed_run(case.case_id, approach, mode, redact(f"{type(exc).__name__}: {exc}")[:300],
                            latency=round(time.perf_counter() - started, 3), model_calls=llm.call_count, tokens=llm.tokens())
    return record, llm


def run_evaluation(
    mode: str,
    make_llm: Callable[[], LLMClient],
    *,
    approaches: tuple[Approach, ...] = APPROACHES,
    case_ids: list[str] | None = None,
    agent_config: AgentConfig | None = None,
    baseline_config: BaselineConfig | None = None,
    eval_dir: Path = EVAL_DIR,
    model_name: str | None = None,
) -> dict:
    cases = [c for c in load_cases(eval_dir) if case_ids is None or c.case_id in case_ids]
    agent_config = agent_config or AgentConfig()
    baseline_config = baseline_config or BaselineConfig()

    runs: list[RunRecord] = []
    for case in cases:  # sequential: one investigation at a time, so latencies are not shared
        for approach in approaches:
            record, _ = asyncio.run(run_case(case, approach, make_llm, mode, agent_config, baseline_config))
            runs.append(record)

    labels = load_labels(eval_dir)  # loaded only now, after every investigation has finished
    scores = [score(r, labels[r.case_id]) for r in runs]
    return {
        "meta": {
            "mode": mode,
            "model": model_name,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "cases": [c.case_id for c in cases],
            "approaches": list(approaches),
            "agent_config": agent_config.model_dump(mode="json"),
            "baseline_config": baseline_config.model_dump(mode="json"),
        },
        "aggregates": {
            a: aggregate([r for r in runs if r.approach == a], [s for s in scores if s.approach == a]) for a in approaches
        },
        "scores": [s.model_dump(mode="json") for s in scores],
        "runs": [r.model_dump(mode="json") for r in runs],
        "labels": {cid: labels[cid].model_dump(mode="json") for cid in sorted({r.case_id for r in runs})},
    }
