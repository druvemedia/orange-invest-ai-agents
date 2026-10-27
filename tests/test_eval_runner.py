"""The evaluation harness end to end, offline: fairness, label isolation, repeatability."""

import asyncio

import pytest

from app.agents import AgentConfig, OrchestratorAgent
from app.baseline import BaselineConfig
from app.evaluation import __main__ as cli
from app.evaluation import runner
from app.evaluation.dataset import load_cases, load_labels
from app.evaluation.heuristic_model import HeuristicLLMClient
from app.evaluation.report import render_markdown
from app.evaluation.runner import run_case, run_evaluation

CASES = load_cases()
LABELS = load_labels()


@pytest.fixture(scope="module")
def offline():
    return run_evaluation("offline-heuristic", HeuristicLLMClient)


def _stable(result):
    runs = [{k: v for k, v in r.items() if k != "latency_seconds"} for r in result["runs"]]
    aggs = {a: {k: v for k, v in agg.items() if k != "latency_seconds"} for a, agg in result["aggregates"].items()}
    return runs, result["scores"], aggs


def test_offline_evaluation_is_repeatable(offline):
    assert _stable(offline) == _stable(run_evaluation("offline-heuristic", HeuristicLLMClient))


def test_both_approaches_run_on_the_identical_case_set(offline):
    by_approach = {a: [r["case_id"] for r in offline["runs"] if r["approach"] == a] for a in runner.APPROACHES}

    assert by_approach["multi_agent"] == by_approach["single_agent"] == [c.case_id for c in CASES]
    assert len(offline["scores"]) == 2 * len(CASES)


def test_simulated_tool_failure_applies_to_both_approaches(offline):
    for r in offline["runs"]:
        if r["case_id"] == "EVAL-07":
            outcomes = {info["outcome"] for ref, info in r["evidence"].items() if info["kind"] == "result" and info["text"] == "query_metrics"}
            assert outcomes == {"failed"}, r["approach"]


@pytest.mark.parametrize("approach", runner.APPROACHES)
def test_gold_labels_never_reach_the_investigation(monkeypatch, approach):
    def forbidden(*args, **kwargs):
        raise AssertionError("labels loaded during an investigation")

    monkeypatch.setattr(runner, "load_labels", forbidden)
    for case in CASES:
        record, llm = asyncio.run(run_case(case, approach, HeuristicLLMClient, "test", AgentConfig(), BaselineConfig()))
        assert record.status != "failed"
        assert len(llm.instructions) == len(llm.inputs) == llm.call_count > 0
        sent = "\n".join(llm.inputs + llm.instructions)  # everything sent to the model: prompts and inputs
        label = LABELS[case.case_id]
        for text in [label.rationale, "expected_outcome", "acceptable_alternatives", "inconclusive_acceptable",
                     *(([label.root_cause.summary, label.root_cause.label_id]) if label.root_cause else [])]:
            assert text not in sent, (case.case_id, approach, text[:40])
        from tests.test_prompt_leakage import leaks
        for instructions in set(llm.instructions):  # the prompts actually sent, not only the templates in the code
            assert leaks(instructions) == [], (case.case_id, approach)


def test_a_crashing_run_is_recorded_as_failed(monkeypatch):
    async def crash(self, incident, investigation_id=None):
        raise RuntimeError("boom with sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    monkeypatch.setattr(OrchestratorAgent, "investigate", crash)
    result = run_evaluation("test", HeuristicLLMClient, approaches=("multi_agent",), case_ids=["EVAL-01", "EVAL-02"])

    assert [r["status"] for r in result["runs"]] == ["failed", "failed"]
    assert result["aggregates"]["multi_agent"]["failure_rate"] == 1.0
    assert "sk-proj-ABCDEF" not in result["runs"][0]["error"]


def test_offline_results_are_labelled_as_not_measuring_llm_quality(offline):
    md = render_markdown(offline)

    assert "OFFLINE results produced with a rule-based stand-in" in md
    assert "say **nothing** about the quality of real LLM reasoning" in md
    assert offline["aggregates"]["multi_agent"]["tokens"] is None  # never invented


def test_live_mode_refuses_without_explicit_confirmation(clean_env, monkeypatch, capsys, tmp_path):
    from app.core.config import Settings

    def no_calls(*args, **kwargs):
        raise AssertionError("a client must not be built without --confirm-billed")

    monkeypatch.setattr("app.llm.build_llm_client", no_calls)
    # Hermetic: an explicitly configured, opted-in model, so only the missing --confirm-billed can stop the run.
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(
        _env_file=None, llm_provider="openai", llm_model="gpt-4o-mini", llm_api_key="sk-test-not-a-real-key",
        llm_allow_billed_calls=True))

    assert cli.main(["live", "--cases", "EVAL-01", "--max-cost-usd", "1", "--out", str(tmp_path / "new")]) == 2
    assert "Refusing to make billed calls without --confirm-billed" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


def test_labels_are_loaded_only_after_every_investigation(monkeypatch):
    events = []
    real_run_case, real_load_labels = runner.run_case, runner.load_labels

    async def tracked_run_case(case, *args, **kwargs):
        assert "labels" not in events, "an investigation ran after labels were loaded"
        events.append("run")
        return await real_run_case(case, *args, **kwargs)

    def tracked_load_labels(*args, **kwargs):
        events.append("labels")
        return real_load_labels(*args, **kwargs)

    monkeypatch.setattr(runner, "run_case", tracked_run_case)
    monkeypatch.setattr(runner, "load_labels", tracked_load_labels)
    run_evaluation("test", HeuristicLLMClient, case_ids=["EVAL-01", "EVAL-09"])

    assert events == ["run"] * 4 + ["labels"]
