"""Runs both approaches on the same cases, then scores them.

Investigation runs see only ``EvalCase.incident`` and ``EvalCase.tools()``. Gold labels are loaded
after every run has finished, and only for scoring. Runs are sequential, each case and approach exactly once:
nothing is retried as a whole. With a ``Budget``, a run starts only if the estimated spend so far plus that run's
worst-case cost fits within the budget; every run not started is recorded as ``not_run`` and scored, never dropped.
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
from app.evaluation.normalize import Approach, RunRecord, failed_run, from_multi_agent, from_single_agent, not_run
from app.evaluation.pricing import PRICES, estimate_cost
from app.schemas.common import StrictModel
from app.llm import LLMClient, LLMRequest

APPROACHES: tuple[Approach, ...] = ("multi_agent", "single_agent")


class MeteredLLM:
    """Counts model calls and keeps every request's instructions and input in memory (used by the label-leak test)."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.call_count = 0
        self.inputs: list[str] = []
        self.instructions: list[str] = []

    async def generate(self, request: LLMRequest, output_type):
        self.call_count += 1
        self.inputs.append(request.input_json)
        self.instructions.append(request.instructions)
        return await self.inner.generate(request, output_type)

    def tokens(self) -> tuple[int | None, int | None]:
        stats = [c for c in getattr(self.inner, "calls", []) if getattr(c, "input_tokens", None) is not None]
        if not stats:
            return None, None
        return sum(c.input_tokens for c in stats), sum(c.output_tokens or 0 for c in stats)


class Budget(StrictModel):
    """A spending limit for billed runs. Worst cases come from the configured request and token limits."""

    max_usd: float
    worst_case_usd: dict[str, float]  # per approach: the most one investigation can cost under the limits


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
                         model_calls=llm.call_count, tokens=llm.tokens(),
                         affected_services=list(case.incident.affected_services))
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
    model_config: dict | None = None,
    budget: Budget | None = None,
) -> dict:
    cases = [c for c in load_cases(eval_dir) if case_ids is None or c.case_id in case_ids]
    agent_config = agent_config or AgentConfig()
    baseline_config = baseline_config or BaselineConfig()

    runs: list[RunRecord] = []
    spent, stopped = 0.0, None
    for case in cases:  # sequential: one investigation at a time, so latencies are not shared
        for approach in approaches:
            if budget is not None and stopped is None and spent + budget.worst_case_usd[approach] > budget.max_usd:
                stopped = (f"budget stop before {case.case_id} {approach}: estimated spend ${spent:.4f} plus this "
                           f"run's worst case ${budget.worst_case_usd[approach]:.4f} exceeds ${budget.max_usd:.2f}")
            if stopped:
                runs.append(not_run(case.case_id, approach, mode, stopped))
                continue
            record, _ = asyncio.run(run_case(case, approach, make_llm, mode, agent_config, baseline_config))
            record = record.model_copy(update={"estimated_cost_usd": estimate_cost(model_name, record.input_tokens,
                                                                                   record.output_tokens)})
            # Unknown usage counts as the worst case, so the budget is never under-counted.
            spent += (record.estimated_cost_usd if record.estimated_cost_usd is not None
                      else (budget.worst_case_usd[approach] if budget else 0.0))
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
            "model_config": model_config,
            "pricing": PRICES[model_name].model_dump() if model_name in PRICES else None,
            "budget": ({"max_usd": budget.max_usd, "worst_case_usd": budget.worst_case_usd,
                        "estimated_spend_usd": round(spent, 6), "stopped": stopped} if budget else None),
        },
        "aggregates": {
            a: aggregate([r for r in runs if r.approach == a], [s for s in scores if s.approach == a]) for a in approaches
        },
        "scores": [s.model_dump(mode="json") for s in scores],
        "runs": [r.model_dump(mode="json") for r in runs],
        "labels": {cid: labels[cid].model_dump(mode="json") for cid in sorted({r.case_id for r in runs})},
    }
