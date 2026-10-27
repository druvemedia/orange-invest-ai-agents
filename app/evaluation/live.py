"""Safeguards for the billed live evaluation: a preflight estimate, a budget, and a provenance record.

Nothing here makes a model call. The worst case per investigation comes from the configured limits: the most model
requests an approach can make, each at the full per-request input budget and output limit. That bound holds only
if the provider's own per-request overhead stays within the input budget's allowances (see
``app/llm/openai_client.py``); it is stated as such everywhere it is shown.
"""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.agents import AgentConfig
from app.baseline import BaselineConfig
from app.core.config import Settings
from app.evaluation.pricing import PRICES
from app.evaluation.runner import Budget

# Measured costs of the four real multi-agent runs of EVAL-01 (gpt-4o-mini; artifacts/live-runs/). The single-agent
# baseline has never been run on a real model, so its expected cost is unknown.
MEASURED_MULTI_AGENT_USD = (0.0113, 0.0090, 0.0161, 0.0121)


def max_requests(approach: str, agent: AgentConfig, baseline: BaselineConfig) -> int:
    """The most model requests one investigation can make under the configured limits."""
    if approach == "multi_agent":
        # orchestrate, observability, dependency, report, plus a reasoning and a critic task per round
        return (4 + 2 * agent.max_critic_rounds) * agent.agent_retry.max_attempts
    return baseline.max_steps * baseline.model_attempts_per_step


def preflight(settings: Settings, approaches, n_cases: int, max_usd: float) -> dict:
    """Plan, worst case and expected cost. Raises ValueError when the run must not start."""
    model = settings.llm_model.strip()
    price = PRICES.get(model)
    if price is None:
        raise ValueError(f"no price is recorded for model '{model}', so its cost cannot be bounded "
                         "(add it to app/evaluation/pricing.py)")
    agent, baseline = AgentConfig.from_settings(settings), BaselineConfig.from_settings(settings)
    per_request = (settings.llm_max_input_tokens * price.input_per_million
                   + settings.llm_max_output_tokens * price.output_per_million) / 1e6
    requests = {a: max_requests(a, agent, baseline) for a in approaches}
    worst = {a: round(requests[a] * per_request, 4) for a in approaches}
    if max_usd < max(worst.values()):
        raise ValueError(f"the budget ${max_usd:.2f} is below the worst case of a single investigation "
                         f"(${max(worst.values()):.4f}), so not even one run can be guaranteed within it")
    measured = sum(MEASURED_MULTI_AGENT_USD) / len(MEASURED_MULTI_AGENT_USD)
    return {
        "model": model,
        "investigations": n_cases * len(approaches),
        "max_requests_per_investigation": requests,
        "max_requests_total": n_cases * sum(requests.values()),
        "worst_case_per_investigation_usd": worst,
        "worst_case_total_usd": round(n_cases * sum(worst.values()), 4),
        "expected_multi_agent_usd": round(measured, 4),
        "expected_total_usd_rough": round(n_cases * len(approaches) * measured, 4),
        "max_usd": max_usd,
        "budget": Budget(max_usd=max_usd, worst_case_usd=worst),
        "limits": {"llm_max_input_tokens": settings.llm_max_input_tokens,
                   "llm_max_output_tokens": settings.llm_max_output_tokens,
                   "llm_timeout_seconds": settings.llm_timeout_seconds,
                   "agent_max_critic_rounds": settings.agent_max_critic_rounds,
                   "agent_max_evidence_requests": settings.agent_max_evidence_requests,
                   "investigation_deadline_seconds": settings.investigation_deadline_seconds},
    }


def describe(plan: dict) -> str:
    lines = [
        f"Live evaluation plan: {plan['investigations']} investigations with model '{plan['model']}', run one at a time,",
        f"each exactly once (no investigation is ever retried as a whole).",
        f"  most model requests: " + ", ".join(f"{a} {n}" for a, n in plan["max_requests_per_investigation"].items())
        + f" per investigation; {plan['max_requests_total']} in total",
        f"  worst case: " + ", ".join(f"{a} ${v:.4f}" for a, v in plan["worst_case_per_investigation_usd"].items())
        + f" per investigation; ${plan['worst_case_total_usd']:.2f} for all, assuming the provider's per-request "
          "overhead stays within the input budget's allowances",
        f"  expected: about ${plan['expected_total_usd_rough']:.2f} in total (the mean of four measured multi-agent "
        f"runs, ${plan['expected_multi_agent_usd']:.4f}, applied to every run; the baseline's real cost is unmeasured)",
        f"  budget: ${plan['max_usd']:.2f}. A run starts only if spend so far plus its worst case fits; the rest are "
        "recorded as not_run.",
    ]
    return "\n".join(lines)


def unique_output_dir(root: Path, requested: Path | None) -> Path:
    """A new directory for this run. Raises FileExistsError rather than overwriting earlier results."""
    out = requested or root / "evaluation_results" / "live" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if out.exists():
        raise FileExistsError(f"{out} already exists; live results are never overwritten")
    return out


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_provenance(out: Path, result: dict, plan: dict) -> None:
    """README.md with provenance, configuration, limits, outcomes and limitations, then SHA256SUMS for every file."""
    meta, aggs = result["meta"], result["aggregates"]
    dirty = _git("status", "--porcelain")
    lines = [
        f"# Live evaluation, {meta['generated_at']}",
        "",
        "Real model calls (billed). One run per case and approach, no repetitions: differences of one or two cases",
        "are noise. Gold labels were loaded only after every investigation had finished.",
        "",
        "## Provenance",
        "",
        f"- Code: commit `{_git('rev-parse', 'HEAD')}`" + (" with uncommitted changes" if dirty and dirty != "unknown" else ""),
        f"- Model: `{plan['model']}`; limits: " + ", ".join(f"{k} {v}" for k, v in plan["limits"].items()),
        f"- Budget ${plan['max_usd']:.2f}; estimated spend ${meta['budget']['estimated_spend_usd']:.4f}"
        + (f"; stopped early: {meta['budget']['stopped']}" if meta["budget"]["stopped"] else "; not stopped"),
        f"- Pricing: {json.dumps(meta['pricing'])}",
        "",
        "## Outcomes",
        "",
    ]
    for approach, a in aggs.items():
        c = a["counts"]
        lines.append(f"- **{approach}**: {a['runs_completed']}/{a['cases']} completed ({a['runs_not_run']} not run); "
                     f"strict {c['strict']['n']}/{c['strict']['of']}, lenient {c['lenient']['n']}/{c['lenient']['of']}, "
                     f"wrong {c['wrong_conclusion']['n']}/{c['wrong_conclusion']['of']}; outcomes {a['outcomes']}")
    lines += ["", "## Limitations", "",
              "- 10 small synthetic cases, one run each; no statistical significance.",
              "- Costs are estimates from reported token usage at standard prices.",
              "- Accuracy is scored by keyword patterns in the gold labels (docs/EVALUATION.md).",
              "", "Files: `results.json`, `per_case.csv`, `report.md`; checksums in `SHA256SUMS`.", ""]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")
    sums = [f"{hashlib.sha256((out / n).read_bytes()).hexdigest()}  {n}"
            for n in ("results.json", "per_case.csv", "report.md", "README.md")]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
