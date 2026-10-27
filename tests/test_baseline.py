"""Single-agent baseline: tool loop, validation and rules, with a scripted model (no network)."""

import asyncio
import json

from app.baseline import BaselineConfig, SingleAgentInvestigator
from app.evaluation.dataset import load_cases
from app.llm import LLMError, LLMErrorKind, ScriptedLLMClient
from app.tools import TOOL_CLASSES, FaultInjector, FixtureStore, SimulatedFault, build_evidence_tools

CASE = {c.case_id: c for c in load_cases()}["EVAL-01"]
WINDOW = {"start": "2026-09-01T08:40:00Z", "end": "2026-09-01T10:57:00Z"}


def call(tool, query, reason="r"):
    return {"tool": tool, "query_json": query if isinstance(query, str) else json.dumps(query), "reason": reason}


def tools_step(*calls):
    return {"action": "call_tools", "tool_calls": list(calls), "report": None}


def refs_where(request, predicate):
    data = json.loads(request.input_json)
    return [i["ref"] for s in data["evidence"] for i in s["items"] if predicate(i["record"])]


def report_step(conclusion="probable_cause_identified", confidence="medium", refs_fn=None, contra=None):
    def respond(request):
        support = refs_fn(request) if refs_fn else []
        return {"action": "final_report", "tool_calls": [], "report": {
            "conclusion": conclusion, "headline": "h", "summary": "s",
            "ranked_hypotheses": [
                {"rank": 1, "hypothesis_id": "H1", "title": "Pool size reduced", "confidence": confidence,
                 "supporting_refs": support, "contradicting_refs": contra or [], "assessment": "a"},
                {"rank": 2, "hypothesis_id": "H2", "title": "Something else", "confidence": "low",
                 "supporting_refs": [], "contradicting_refs": [], "assessment": "b"}],
            "facts": [{"text": "uncited", "evidence_refs": []}], "interpretations": [], "assumptions": [],
            "would_confirm": [], "would_disconfirm": [], "limitations": [],
            "corrective_actions": [], "open_questions": []}}
    return respond


def pool_and_change(request):
    return (refs_where(request, lambda r: r.get("change_id") == "CHG-E01-2")
            + refs_where(request, lambda r: "pool" in r.get("message", "")))


def run(script, config=None, faults=None):
    llm = ScriptedLLMClient({"baseline": script})
    tools = build_evidence_tools(FixtureStore(CASE.data_dir), FaultInjector(faults or {}))
    result = asyncio.run(SingleAgentInvestigator(llm, tools, config).investigate(CASE.incident, "base-test"))
    return result, llm


SWEEP = tools_step(call("search_logs", {**WINDOW, "min_level": "WARNING"}),
                   call("get_change_history", {"start": "2026-08-31T08:00:00Z", "end": WINDOW["end"]}))


def test_baseline_gathers_evidence_then_reports():
    result, llm = run([SWEEP, report_step(refs_fn=pool_and_change)])

    assert result.status == "completed"
    assert [s.ref for s in result.evidence] == ["b1.1", "b1.2"]
    assert result.tool_executions == 2 and result.model_calls == 2
    h1 = result.report.report.ranked_hypotheses[0]
    assert h1.confidence == "medium" and result.report.report.conclusion == "probable_cause_identified"
    assert all(ref.startswith(("b1.1#", "b1.2#")) for ref in h1.supporting_refs)
    assert "uncited" in result.report.report.assumptions  # uncited fact moved, same rule as multi-agent
    second_input = json.loads(llm.calls_for("baseline")[1].input_json)
    assert second_input["step"] == 2 and len(second_input["evidence"]) == 2


def test_baseline_sees_exactly_the_registered_tools():
    _, llm = run([SWEEP, report_step(refs_fn=pool_and_change)])
    first = json.loads(llm.calls_for("baseline")[0].input_json)

    assert set(first["available_tools"]) == {cls.name for cls in TOOL_CLASSES}
    assert "labels" not in llm.calls[0].input_json and "expected_outcome" not in llm.calls[0].input_json


def test_invalid_and_repeated_tool_calls_are_rejected_individually_and_reported_back():
    step1 = tools_step(call("rm_rf", {}), call("search_logs", "{bad"), call("search_logs", {**WINDOW}),
                       call("search_logs", {**WINDOW}))
    result, llm = run([step1, tools_step(call("search_logs", {**WINDOW})), report_step(conclusion="inconclusive")])

    assert [s.ref for s in result.evidence] == ["b1.1"]  # only the one valid, first-time call ran
    reasons = [r.reason for r in result.steps[0].rejected_calls]
    assert reasons[0] == "unknown tool 'rm_rf'"
    assert reasons[1].startswith("invalid query: JSONDecodeError")
    assert reasons[2] == "the same query was already run"
    fed_back = json.loads(llm.calls_for("baseline")[1].input_json)["rejected_tool_calls"]
    assert len(fed_back) == 3
    assert result.steps[1].rejected_calls[0].reason == "the same query was already run"  # across steps too


def test_tool_call_budget_is_enforced():
    many = tools_step(*[call("search_logs", {**WINDOW, "keywords": [f"k{i}"]}) for i in range(5)])
    result, _ = run([many, report_step(conclusion="inconclusive")],
                    config=BaselineConfig(max_tool_calls=3, max_tool_calls_per_step=6))

    assert len(result.evidence) == 3
    assert [r.reason for r in result.steps[0].rejected_calls] == ["exceeds the tool-call budget"] * 2


def test_invented_reference_is_rejected_then_retried():
    def invents(request):
        out = report_step(refs_fn=lambda r: ["b1.1#999"])(request)
        return out

    result, llm = run([SWEEP, invents, report_step(refs_fn=pool_and_change)])

    assert result.status == "completed"
    assert result.invalid_outputs == 1
    assert len(llm.calls_for("baseline")) == 3


def test_failed_tool_evidence_cannot_be_used_as_support():
    def cites_failed(request):
        return report_step(refs_fn=lambda r: ["b1.1"])(request)

    result, _ = run([tools_step(call("search_logs", WINDOW)), cites_failed, cites_failed],
                    faults={"search_logs": SimulatedFault.UNAVAILABLE})

    assert result.evidence[0].outcome == "failed"
    assert result.tool_executions == 2  # retryable simulated fault: retried once
    assert result.status == "fallback" and result.invalid_outputs == 2
    assert any(lim.startswith("b1.1 (search_logs) failed: no evidence was obtained") for lim in result.report.report.limitations)


def test_confidence_cap_and_conclusion_rule_match_the_multi_agent_rules():
    result, _ = run([SWEEP, report_step(confidence="high", refs_fn=lambda r: pool_and_change(r)[:1])])

    h1 = result.report.report.ranked_hypotheses[0]
    assert h1.confidence == "medium"  # high needs two independent sources
    assert any("not independently corroborated" in a for a in result.report.adjustments)

    tied, _ = run([SWEEP, report_step(confidence="low", refs_fn=pool_and_change)])
    assert tied.report.report.conclusion == "inconclusive"


def test_must_report_at_last_step_or_fall_back():
    keep_calling = tools_step(call("search_logs", {**WINDOW, "keywords": ["x"]}))
    result, llm = run([keep_calling], config=BaselineConfig(max_steps=2))

    assert result.status == "fallback"
    assert result.report.generated_by == "fallback" and result.report.report.conclusion == "inconclusive"
    assert len(llm.calls_for("baseline")) == 3  # step 1, then step 2 twice (invalid: report required)


def test_permanent_model_error_is_not_retried():
    rejected = LLMError(LLMErrorKind.REJECTED, "The model provider rejected the credentials (HTTP 401).")
    result, llm = run([rejected])

    assert result.status == "fallback" and len(llm.calls_for("baseline")) == 1
    assert any("HTTP 401" in lim for lim in result.report.report.limitations)
