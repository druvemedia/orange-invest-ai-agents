"""The fair-evaluation additions: budget stop, cost estimates, denominators, provenance. No model is ever called."""

import hashlib
import re

import pytest

from app.agents import AgentConfig
from app.baseline import BaselineConfig
from app.core.config import Settings
from app.evaluation import runner
from app.evaluation.heuristic_model import HeuristicLLMClient
from app.evaluation.live import max_requests, preflight, unique_output_dir, write_provenance
from app.evaluation.metrics import aggregate, score
from app.evaluation.pricing import estimate_cost
from app.evaluation.report import render_markdown

OPENAI = {"llm_provider": "openai", "llm_model": "gpt-4o-mini", "llm_api_key": "sk-test-not-a-real-key",
          "llm_allow_billed_calls": True}


class Usage(HeuristicLLMClient):
    """The offline stand-in, reporting a fixed, known usage per call, as a real client would."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def generate(self, request, output_type):
        out = await super().generate(request, output_type)
        self.calls.append(type("Stats", (), {"input_tokens": 10_000, "output_tokens": 500})())
        return out


def test_cost_is_estimated_only_for_a_priced_model_with_reported_usage():
    assert estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000) == 0.75
    assert estimate_cost("gpt-4o-mini", None, None) is None  # offline: usage not reported
    assert estimate_cost("some-unpriced-model", 1000, 100) is None


def test_the_budget_stops_runs_and_records_every_skipped_run():
    budget = runner.Budget(max_usd=0.05, worst_case_usd={"multi_agent": 0.03, "single_agent": 0.03})

    result = runner.run_evaluation("test", Usage, case_ids=["EVAL-01", "EVAL-02", "EVAL-03"], model_name="gpt-4o-mini",
                                   budget=budget)

    statuses = [(r["case_id"], r["approach"], r["status"]) for r in result["runs"]]
    assert len(statuses) == 6  # every planned run is recorded, none dropped
    ran = [s for s in statuses if s[2] != "not_run"]
    assert ran and all(r["estimated_cost_usd"] for r in result["runs"] if r["status"] != "not_run")
    assert result["meta"]["budget"]["stopped"].startswith("budget stop before")
    assert result["meta"]["budget"]["estimated_spend_usd"] + 0.03 > 0.05  # stopped only when the next could exceed
    for approach, agg in result["aggregates"].items():
        assert agg["cases"] == 3 and agg["runs_completed"] + agg["runs_not_run"] == 3
        assert agg["counts"]["strict"]["of"] == 3  # not-run runs stay in the denominator
        assert agg["outcomes"]["not_run"] == agg["runs_not_run"]


def test_unknown_usage_is_charged_at_the_worst_case():
    budget = runner.Budget(max_usd=0.05, worst_case_usd={"multi_agent": 0.03, "single_agent": 0.03})

    result = runner.run_evaluation("test", HeuristicLLMClient, case_ids=["EVAL-01", "EVAL-02"], budget=budget)

    assert [r["status"] for r in result["runs"]].count("not_run") == 3  # 0.03 charged, next 0.03 would exceed 0.05


def test_offline_runs_count_unsupported_citations_gaps_and_report_denominators():
    result = runner.run_evaluation("offline-heuristic", HeuristicLLMClient, case_ids=["EVAL-07"])

    for r in result["runs"]:
        assert r["assessed_supporting_refs"] > 0 and r["unsupported_supporting_refs"] <= r["assessed_supporting_refs"]
        assert r["evidence_gaps"] >= 1  # EVAL-07's metrics tool is simulated as down
        assert r["estimated_cost_usd"] is None  # no usage offline: unknown, never invented
    md = render_markdown(result)
    strict_row = next(line for line in md.splitlines() if line.startswith("| Strict accuracy |"))
    assert re.fullmatch(r"\| Strict accuracy \| \d+% \([01]/1\) \| \d+% \([01]/1\) \|", strict_row), strict_row
    assert "n/a (no price or usage; 0/1)" in md


def test_preflight_counts_requests_from_the_configured_limits():
    settings = Settings(_env_file=None, **OPENAI)

    plan = preflight(settings, ("multi_agent", "single_agent"), 10, max_usd=5.0)

    assert max_requests("multi_agent", AgentConfig(), BaselineConfig()) == 16  # as measured with a hostile model
    assert plan["max_requests_per_investigation"] == {"multi_agent": 16, "single_agent": 12}
    per_request = (64_000 * 0.15 + 4_000 * 0.60) / 1e6
    assert plan["worst_case_per_investigation_usd"] == {"multi_agent": round(16 * per_request, 4),
                                                        "single_agent": round(12 * per_request, 4)}
    assert plan["investigations"] == 20 and plan["budget"].max_usd == 5.0


def test_preflight_refuses_unpriced_models_and_budgets_below_one_run():
    with pytest.raises(ValueError, match="no price"):
        preflight(Settings(_env_file=None, **{**OPENAI, "llm_model": "unpriced-model"}), ("multi_agent",), 1, 5.0)
    with pytest.raises(ValueError, match="below the worst case"):
        preflight(Settings(_env_file=None, **OPENAI), ("multi_agent",), 1, 0.01)


def test_output_directories_are_unique_and_never_reused(tmp_path):
    fresh = unique_output_dir(tmp_path, None)
    assert fresh.parent == tmp_path / "evaluation_results" / "live" and not fresh.exists()

    taken = tmp_path / "taken"
    taken.mkdir()
    with pytest.raises(FileExistsError):
        unique_output_dir(tmp_path, taken)


def test_provenance_readme_and_checksums_cover_every_file(tmp_path):
    from app.evaluation.report import write_outputs

    budget = runner.Budget(max_usd=1.0, worst_case_usd={"multi_agent": 0.2, "single_agent": 0.2})
    result = runner.run_evaluation("live", Usage, case_ids=["EVAL-09"], model_name="gpt-4o-mini", budget=budget,
                                   model_config={"llm_max_input_tokens": 64000})
    plan = preflight(Settings(_env_file=None, **OPENAI), ("multi_agent", "single_agent"), 1, max_usd=1.0)
    out = tmp_path / "run"
    write_outputs(result, out)
    write_provenance(out, result, plan)

    readme = (out / "README.md").read_text()
    assert "Code: commit" in readme and "gpt-4o-mini" in readme and "Limitations" in readme and "not stopped" in readme
    sums = dict(line.split("  ")[::-1] for line in (out / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == {"results.json", "per_case.csv", "report.md", "README.md"}
    for name, digest in sums.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


def test_scores_treat_not_run_as_its_own_outcome():
    from app.evaluation.dataset import load_labels
    from app.evaluation.normalize import not_run

    run = not_run("EVAL-01", "multi_agent", "live", "budget stop")
    s = score(run, load_labels()["EVAL-01"])

    assert s.outcome == "not_run" and not s.strict_correct and not s.lenient_correct
    assert aggregate([run], [s])["counts"]["strict"] == {"n": 0, "of": 1}
