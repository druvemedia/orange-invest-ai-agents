"""Six-agent investigations over the shared fixtures, with a scripted (fake) language model.

No test makes a network call or needs an API key.
"""

import asyncio
import json
import re

import pytest

from app.agents import AgentConfig, OrchestratorAgent
from app.agents.contracts import (
    CriticInput,
    CriticResult,
    DependencyInput,
    DependencyResult,
    ObservabilityInput,
    ObservabilityResult,
    ReasoningInput,
    ReasoningResult,
    ReportingInput,
)
from app.agents.base import read_envelope, read_result
from app.llm import LLMError, LLMErrorKind, ScriptedLLMClient
from app.orchestration import EngineLimits, InvestigationStatus, TaskErrorCode, TaskStatus, TerminationReason
from app.schemas.incident import IncidentReport
from app.tools import FaultInjector, FixtureStore, SimulatedFault, build_evidence_tools
from tests import agent_scripts as s

INCIDENTS = {r.incident_id: r for r in FixtureStore().load("incidents.jsonl", IncidentReport).records}
INC_2041 = INCIDENTS["INC-2041"]
CRITIC_EVIDENCE = re.compile(r"^ev\.c\d+\.\d+")  # evidence tasks requested by the critic
ALL_TOOLS = ["search_logs", "query_metrics", "get_change_history", "get_service_dependencies", "search_historical_incidents"]


def investigate(scripts, *, faults=None, incident=INC_2041, config=None, llm=None):
    llm = llm or ScriptedLLMClient(scripts)
    tools = build_evidence_tools(FixtureStore(), FaultInjector(faults or {}))
    orchestrator = OrchestratorAgent(llm, tools, config or AgentConfig())
    result = asyncio.run(orchestrator.investigate(incident, investigation_id="inv-test"))
    return result, llm


def input_of(llm, agent, model, call=0):
    return model.model_validate_json(llm.calls_for(agent)[call].input_json)


def resolve(ref: str, tasks) -> None:
    """Assert that a reference leads back to a real tool record or result."""
    if "/" in ref:
        task_id, finding = ref.split("/", 1)
        result = read_envelope(tasks[task_id]).result["output"]
        found = [f for f in result.get("observations", []) + result.get("findings", [])
                 if finding in (f.get("observation_id"), f.get("finding_id"))]
        assert found, ref
        for inner in found[0]["evidence_refs"]:
            resolve(inner, tasks)
    elif "#" in ref:
        task_id, index = ref.split("#")
        assert tasks[task_id].output.evidence.records[int(index)] is not None
    else:
        assert tasks[ref].spec.kind == "evidence"


# --- The collaborative flow ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def happy():
    return investigate(s.full_script())


def test_full_investigation_completes_with_critic_feedback_round(happy):
    result, _ = happy
    inv = result.investigation

    assert inv.status is InvestigationStatus.COMPLETED
    assert inv.termination_reason is TerminationReason.ALL_TASKS_SUCCEEDED
    assert {"orchestrate", "observability", "dependency", "reasoning.r1", "critic.r1",
            "ev.c1.1", "reasoning.r2", "critic.r2", "report"} <= set(inv.tasks)
    assert inv.tasks["ev.c1.1"].added_by == "critic.r1"
    assert inv.tasks["reasoning.r2"].added_by == "critic.r1"
    assert "reasoning.r3" not in inv.tasks


def test_report_runs_last_and_uses_final_state(happy):
    result, _ = happy
    inv = result.investigation
    report_start = inv.tasks["report"].started_at
    assert all(t.finished_at <= report_start for tid, t in inv.tasks.items() if tid != "report")

    report = result.report
    assert report.generated_by == "model"
    assert report.critic_rounds == 2 and report.final_critic_verdict == "accept"
    assert report.report.conclusion == "probable_cause_identified"
    assert [h.hypothesis_id for h in report.report.ranked_hypotheses] == ["H1", "H2"]


def test_critic_requested_evidence_reaches_reasoning(happy):
    result, llm = happy
    r2_input = input_of(llm, "reasoning", ReasoningInput, call=1)

    assert r2_input.round == 2
    assert r2_input.new_evidence_refs == ["ev.c1.1"]
    assert [i.issue_id for i in r2_input.critique] == ["C1"]
    new_items = [i for es in r2_input.evidence if es.ref == "ev.c1.1" for i in es.items]
    assert any("Config reloaded" in i.record["message"] for i in new_items)

    r2 = read_result(result.investigation.tasks["reasoning.r2"], ReasoningResult)
    h1 = next(h for h in r2.output.hypotheses if h.hypothesis_id == "H1")
    assert any(ref.startswith("ev.c1.1#") for ref in h1.supporting_refs)
    assert r2.output.critique_responses


def test_reasoning_consumes_both_evidence_agents(happy):
    _, llm = happy
    r1_input = input_of(llm, "reasoning", ReasoningInput)

    assert r1_input.observability is not None and r1_input.dependency is not None
    assert {o.observation_id for o in r1_input.observability.output.observations} >= {"obs-1", "obs-4"}
    assert {f.finding_id for f in r1_input.dependency.output.findings} >= {"dep-1", "dep-2"}


def test_every_reference_traces_back_to_tool_results(happy):
    result, _ = happy
    tasks = result.investigation.tasks
    refs = [r for h in result.report.report.ranked_hypotheses for r in h.supporting_refs + h.contradicting_refs]
    refs += [r for f in result.report.report.facts for r in f.evidence_refs]
    for tid in ("reasoning.r1", "reasoning.r2"):
        for h in read_result(tasks[tid], ReasoningResult).output.hypotheses:
            refs += h.supporting_refs + h.contradicting_refs
    assert refs
    for ref in refs:
        resolve(ref, tasks)


# --- Per-agent contracts ----------------------------------------------------------------------------


def test_observability_contract(happy):
    result, llm = happy
    inp = input_of(llm, "observability", ObservabilityInput)

    assert {e.ref for e in inp.evidence} == {"ev.logs", "ev.metrics"}
    latency = next(m for m in inp.metric_summaries if m.metric_name == "latency_p95_ms" and m.service == "payments-api")
    assert latency.baseline < 200 < 2000 < latency.peak
    stored = read_result(result.investigation.tasks["observability"], ObservabilityResult)
    assert {o.kind for o in stored.output.observations} == {"fact", "interpretation"}
    assert any("fraud-check" in g for g in stored.data_gaps)  # metrics coverage gap, found by code


def test_dependency_contract_computes_relations_without_causation(happy):
    result, llm = happy
    inp = input_of(llm, "dependency", DependencyInput)
    relations = {c.change_id: c for c in inp.change_relations}

    assert {e.ref for e in inp.evidence} == {"ev.changes", "ev.deps.payments-api"}
    assert relations["CHG-3102"].relation == "focus_service"
    assert relations["CHG-3102"].minutes_before_report == 17.0
    assert relations["CHG-3098"].relation == "no_known_relation"
    assert read_result(result.investigation.tasks["dependency"], DependencyResult).change_relations


def test_critic_contract_offers_only_registered_tools(happy):
    _, llm = happy
    inp = input_of(llm, "critic", CriticInput)

    assert set(inp.available_tools) == set(ALL_TOOLS)
    assert inp.rounds_remaining == 1 and inp.max_evidence_requests == 3
    assert {h.hypothesis_id for h in inp.hypotheses} == {"H1", "H2"}


def test_reporting_contract(happy):
    _, llm = happy
    inp = input_of(llm, "reporting", ReportingInput)

    assert inp.final_reasoning.round == 2
    assert inp.final_critique.final_verdict == "accept"
    assert inp.conclusion_allowed is True and inp.adjudication.decision == "cause_accepted"
    assert {e.task_id for e in inp.evidence_inventory.entries} >= {"ev.logs", "ev.metrics", "ev.c1.1"}
    assert "metrics: payments-api/request_rate_rps" in inp.evidence_inventory.retrieved_signals


# --- Grounding and confidence --------------------------------------------------------------------------


def test_report_cannot_raise_confidence_above_reasoning(happy):
    result, _ = happy
    ranked = {h.hypothesis_id: h.confidence for h in result.report.report.ranked_hypotheses}

    assert ranked == {"H1": "medium", "H2": "low"}  # the scripted report claimed "high" for both
    assert any("lowered" in a for a in result.report.adjustments)


def test_invented_reference_is_rejected_and_retried():
    bad = s.observability
    calls = {"n": 0}

    def first_invents(request):
        calls["n"] += 1
        out = bad(request)
        if calls["n"] == 1:
            out["observations"][0]["evidence_refs"] = ["ev.logs#999"]
        return out

    result, _ = investigate(s.full_script(observability=[first_invents]))
    task = result.investigation.tasks["observability"]

    assert task.status is TaskStatus.SUCCEEDED
    assert len(task.attempts) == 2
    assert task.attempts[0].error.code is TaskErrorCode.MODEL_INVALID_OUTPUT
    assert "ev.logs#999" in task.attempts[0].error.message


def test_confidence_is_capped_for_contradicted_or_single_source_claims():
    def overconfident(request):
        return {
            "hypotheses": [
                {"hypothesis_id": "H1", "title": "Pool", "description": "d",
                 "supporting_refs": ["dependency/dep-1", "observability/obs-1"], "contradicting_refs": ["observability/obs-5"],
                 "missing_information": [], "confidence": "high", "confidence_rationale": "sure"},
                {"hypothesis_id": "H2", "title": "Fraud", "description": "d",
                 "supporting_refs": ["observability/obs-2"], "contradicting_refs": [],
                 "missing_information": [], "confidence": "high", "confidence_rationale": "sure"},
                {"hypothesis_id": "H3", "title": "Speculation", "description": "d",
                 "supporting_refs": ["observability/obs-6"], "contradicting_refs": [],
                 "missing_information": [], "confidence": "medium", "confidence_rationale": "hunch"},
            ],
            "insufficient_evidence": False, "critique_responses": [], "summary": "s",
        }

    result, _ = investigate(s.full_script(reasoning=[overconfident], critic=[s.critic_always_revise]),
                            config=AgentConfig(max_critic_rounds=1))
    env = read_envelope(result.investigation.tasks["reasoning.r1"])
    levels = {h["hypothesis_id"]: h["confidence"] for h in env.result["output"]["hypotheses"]}

    assert levels == {"H1": "medium", "H2": "medium", "H3": "low"}
    assert len(env.adjustments) == 3


def test_critic_rules_reject_unsupported_conclusion_even_if_model_accepts():
    def weak(request):
        return {
            "hypotheses": [{"hypothesis_id": "H1", "title": "Speculation", "description": "d",
                            "supporting_refs": ["observability/obs-6"], "contradicting_refs": [],
                            "missing_information": [], "confidence": "medium", "confidence_rationale": "hunch"}],
            "insufficient_evidence": False, "critique_responses": [], "summary": "s",
        }

    accept = {"verdict": "accept", "issues": [], "evidence_requests": [], "summary": "Looks fine."}
    result, _ = investigate(s.full_script(reasoning=[weak], critic=[accept]), config=AgentConfig(max_critic_rounds=1))
    critic = read_result(result.investigation.tasks["critic.r1"], CriticResult)

    assert critic.output.verdict == "accept"  # what the model said
    assert critic.final_verdict == "revise"  # what the rules decided
    assert critic.output.issues[0].issue_type == "unsupported_claim"
    assert critic.loop_decision == "max_rounds_reached"
    assert result.report.report.conclusion == "inconclusive"  # the scripted report claimed a probable cause
    assert any("inconclusive" in a for a in result.report.adjustments)


def test_competing_hypotheses_with_equal_confidence_stay_inconclusive():
    def tied(request):
        out = s.reasoning(request)
        for h in out["hypotheses"]:
            h["confidence"] = "medium"
        return out

    accept = {"verdict": "accept", "issues": [], "evidence_requests": [], "summary": "ok"}
    result, _ = investigate(s.full_script(reasoning=[tied], critic=[accept]))

    assert result.report.report.conclusion == "inconclusive"
    assert len(result.report.report.ranked_hypotheses) == 2


# --- Critic loop termination -------------------------------------------------------------------------


def test_critic_loop_stops_at_max_rounds():
    result, llm = investigate(s.full_script(critic=[s.critic_always_revise]), config=AgentConfig(max_critic_rounds=3))
    tasks = result.investigation.tasks

    assert [t for t in tasks if t.startswith("critic.")] == ["critic.r1", "critic.r2", "critic.r3"]
    assert "reasoning.r4" not in tasks
    assert len(llm.calls_for("critic")) == 3
    last = read_result(tasks["critic.r3"], CriticResult)
    assert last.loop_decision == "max_rounds_reached"
    assert last.rejected_requests[0].reason == "no critic rounds remaining"
    # Phase 6: the critic still saying "revise" at its limit no longer forces inconclusive by itself.
    # Its issues cite no direct evidence, the leader has direct support from 3 sources and every
    # requested follow-up ran, so the adjudication accepts on evidence and lists the unresolved issues.
    adjudication = result.report.adjudication
    assert adjudication.critic_verdict == "revise" and adjudication.decision == "cause_accepted"
    assert adjudication.basis == "critic verdict 'revise' adjudicated on evidence sufficiency"
    assert adjudication.unresolved_critic_issues and result.report.report.conclusion == "probable_cause_identified"
    assert any("round limit" in lim for lim in result.report.report.limitations)


def test_critic_loop_stops_at_engine_task_budget():
    config = AgentConfig(max_critic_rounds=5, limits=EngineLimits(max_tasks=13))
    result, _ = investigate(s.full_script(critic=[s.critic_always_revise]), config=config)
    inv = result.investigation

    assert len(inv.tasks) <= 13
    assert inv.termination_reason is TerminationReason.TASK_BUDGET_EXHAUSTED
    assert inv.tasks["report"].status is TaskStatus.SUCCEEDED  # the finalizer still reports
    assert any("did not run" in lim for lim in result.report.report.limitations)


def test_invalid_duplicate_and_excess_requests_are_rejected():
    duplicate_of_ev_logs = None

    def critic(request):
        nonlocal duplicate_of_ev_logs
        d = s.payload(request)
        duplicate_of_ev_logs = next(e["query"] for e in d["evidence"] if e["ref"] == "ev.logs")
        reqs = [
            {"tool": "search_logs", "query_json": "{not json", "reason": "r", "hypothesis_id": None},
            {"tool": "query_metrics", "query_json": json.dumps({"start": "2026-10-20T15:00:00Z"}), "reason": "r", "hypothesis_id": None},
            {"tool": "search_logs", "query_json": json.dumps(duplicate_of_ev_logs), "reason": "r", "hypothesis_id": None},
            {"tool": "get_service_dependencies", "query_json": json.dumps({"service": "fraud-check"}), "reason": "r", "hypothesis_id": None},
            {"tool": "get_service_dependencies", "query_json": json.dumps({"service": "api-gateway"}), "reason": "r", "hypothesis_id": None},
        ]
        return {"verdict": "revise", "issues": [], "evidence_requests": reqs, "summary": "s"}

    result, _ = investigate(s.full_script(critic=[critic, {"verdict": "accept", "issues": [], "evidence_requests": [], "summary": "ok"}]),
                            config=AgentConfig(max_evidence_requests=1))
    r1 = read_result(result.investigation.tasks["critic.r1"], CriticResult)
    reasons = [r.reason for r in r1.rejected_requests]

    assert r1.accepted_requests == ["ev.c1.1"]
    assert result.investigation.tasks["ev.c1.1"].spec.inputs["query"]["service"] == "fraud-check"
    assert reasons[0].startswith("invalid query: JSONDecodeError")
    assert reasons[1].startswith("invalid query:") and "end" in reasons[1]
    assert reasons[2] == "the same query was already run"
    assert reasons[3].startswith("exceeds the limit of 1")


# --- Insufficient evidence ----------------------------------------------------------------------------


def test_no_evidence_gives_inconclusive_report_without_guessing():
    faults = {tool: SimulatedFault.UNAVAILABLE for tool in ALL_TOOLS}
    insufficient = {"verdict": "insufficient_evidence", "issues": [], "evidence_requests": [], "summary": "No data."}
    result, llm = investigate(s.full_script(critic=[insufficient]), faults=faults)
    tasks = result.investigation.tasks

    # Rules, not the model, handle agents with nothing to analyse.
    assert llm.calls_for("observability") == [] and llm.calls_for("dependency") == [] and llm.calls_for("reasoning") == []
    assert read_envelope(tasks["reasoning.r1"]).generated_by == "rules"
    r1 = read_result(tasks["reasoning.r1"], ReasoningResult)
    assert r1.output.insufficient_evidence and r1.output.hypotheses == []
    assert read_result(tasks["critic.r1"], CriticResult).loop_decision == "nothing_new_to_investigate"

    report = result.report.report
    assert report.conclusion == "inconclusive"  # scripted report asked for a probable cause
    assert report.ranked_hypotheses == []
    assert any(lim.startswith("ev.logs (search_logs) failed: no evidence was obtained") for lim in report.limitations)
    assert all(e.outcome == "failed" for e in result.investigation.evidence)


def test_incomplete_report_is_scoped_from_model_proposal():
    inc = INCIDENTS["INC-2043"]  # no services, no severity
    window = {"start": "2026-10-24T17:30:00Z", "end": "2026-10-24T19:00:00Z"}
    script = s.full_script(
        orchestrator=[s.scope(window, services=["web-frontend", "Not A Service!"], keywords=["slow", "page"])],
        observability=[{"observations": [], "onset_estimate": None, "evidence_gaps": [], "summary": "x"}],
    )
    result, _ = investigate(script, incident=inc, config=AgentConfig(max_critic_rounds=1))
    scope = read_envelope(result.investigation.tasks["orchestrate"])

    assert scope.result["focus_services"] == ["web-frontend"]
    assert any("Ignored focus services" in a for a in scope.adjustments)
    assert "ev.deps.web-frontend" in result.investigation.tasks
    # observability returned no observations despite records: rejected, retried, then failed
    obs = result.investigation.tasks["observability"]
    assert obs.status is TaskStatus.FAILED and obs.error.code is TaskErrorCode.MODEL_INVALID_OUTPUT


def test_out_of_bounds_scope_is_replaced_by_default():
    far = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-03T00:00:00Z"}
    result, _ = investigate(s.full_script(orchestrator=[s.scope(far)]))
    env = read_envelope(result.investigation.tasks["orchestrate"])

    assert env.result["window_start"] == "2026-10-20T12:42:00Z"
    assert any("default used" in a for a in env.adjustments)


# --- Provider failures --------------------------------------------------------------------------------


def test_transient_provider_error_is_retried():
    rate_limited = LLMError(LLMErrorKind.RATE_LIMITED, "The model provider rate-limited the request (HTTP 429).")
    result, llm = investigate(s.full_script(dependency=[rate_limited, s.dependency]))
    task = result.investigation.tasks["dependency"]

    assert task.status is TaskStatus.SUCCEEDED
    assert [a.error.code if a.error else None for a in task.attempts] == [TaskErrorCode.MODEL_RATE_LIMITED, None]


REJECTED_401 = LLMError(LLMErrorKind.REJECTED, "The model provider rejected the credentials (HTTP 401).")
ACCEPT = {"verdict": "accept", "issues": [], "evidence_requests": [], "summary": "ok"}


def dependency_only_reasoning(request):
    return {
        "hypotheses": [{"hypothesis_id": "H1", "title": "Pool reduction", "description": "d",
                        "supporting_refs": ["dependency/dep-1"], "contradicting_refs": [],
                        "missing_information": ["telemetry"], "confidence": "medium", "confidence_rationale": "r"}],
        "insufficient_evidence": False, "critique_responses": [], "summary": "Based on change history only.",
    }


def assert_permanently_failed(task, code):
    assert task.status is TaskStatus.FAILED
    assert task.output is None
    assert len(task.attempts) == 1  # permanent: not retried
    assert task.error.code is code
    assert task.error.retryable is False


def test_permanent_provider_failure_in_evidence_agent_is_isolated():
    result, llm = investigate(
        s.full_script(observability=[REJECTED_401], reasoning=[dependency_only_reasoning], critic=[ACCEPT])
    )
    tasks = result.investigation.tasks

    assert_permanently_failed(tasks["observability"], TaskErrorCode.MODEL_REJECTED)
    assert tasks["observability"].error.message == REJECTED_401.message
    assert len(llm.calls_for("observability")) == 1
    # Reasoning ran without observability findings, and cites none.
    assert input_of(llm, "reasoning", ReasoningInput).observability is None
    r1 = read_result(tasks["reasoning.r1"], ReasoningResult)
    assert tasks["reasoning.r1"].status is TaskStatus.SUCCEEDED
    assert not any(ref.startswith("observability/") for h in r1.output.hypotheses for ref in h.supporting_refs)
    assert result.investigation.status is InvestigationStatus.PARTIALLY_COMPLETED
    assert result.investigation.termination_reason is TerminationReason.SOME_TASKS_DID_NOT_SUCCEED
    assert any(lim.startswith("Task observability") and "HTTP 401" in lim for lim in result.report.report.limitations)


def test_permanent_provider_failure_in_reasoning_is_never_treated_as_reasoning():
    result, llm = investigate(s.full_script(reasoning=[REJECTED_401]))
    tasks = result.investigation.tasks

    assert_permanently_failed(tasks["reasoning.r1"], TaskErrorCode.MODEL_REJECTED)
    assert read_result(tasks["reasoning.r1"], ReasoningResult) is None
    assert len(llm.calls_for("reasoning")) == 1
    # Nothing to review: the critic fails without calling the model, and no loop starts.
    assert tasks["critic.r1"].status is TaskStatus.FAILED
    assert tasks["critic.r1"].error.code is TaskErrorCode.DEPENDENCY_FAILED
    assert llm.calls_for("critic") == []
    assert "reasoning.r2" not in tasks
    # The report is rule-based and inconclusive, with no hypotheses invented.
    assert llm.calls_for("reporting") == []
    assert result.report.generated_by == "rules"
    assert result.report.report.conclusion == "inconclusive"
    assert result.report.report.ranked_hypotheses == []
    assert result.investigation.status is InvestigationStatus.PARTIALLY_COMPLETED
    assert any(lim.startswith("Task reasoning.r1") for lim in result.report.report.limitations)


# --- Critic evidence-request validation (each request judged on its own) ---------------------------


def request(tool, query, hypothesis_id=None):
    return {"tool": tool, "query_json": query if isinstance(query, str) else json.dumps(query),
            "reason": "test", "hypothesis_id": hypothesis_id}


def critic_round(requests_by_round, verdict="revise"):
    def respond(req):
        round_no = s.payload(req)["round"]
        reqs = requests_by_round.get(round_no)
        if reqs is None:
            return ACCEPT
        return {"verdict": verdict, "issues": [], "evidence_requests": reqs, "summary": "s"}
    return respond


def lenient_reasoning(req):
    out = s.reasoning(req)
    if s.payload(req)["critique"]:
        out["critique_responses"] = out["critique_responses"] or ["Addressed."]
    if s.payload(req)["round"] >= 2 and not s.payload(req)["new_evidence_refs"]:
        for h in out["hypotheses"]:  # no new evidence this round: cite only what exists
            h["supporting_refs"] = [r for r in h["supporting_refs"] if not CRITIC_EVIDENCE.match(r)]
    return out


def test_mixed_valid_and_invalid_requests_keep_the_valid_ones():
    fraud_deps = {"service": "fraud-check"}
    reqs = [
        request("drop_tables", {"table": "payments"}),  # unregistered tool
        request("search_logs", s.POOL_RELOAD_QUERY, "H1"),  # valid
        request("search_logs", {"start": "2026-10-20T13:30:00Z"}),  # schema-invalid: no end
        request("get_service_dependencies", fraud_deps, "H9"),  # unknown hypothesis
        request("get_service_dependencies", fraud_deps, "H2"),  # valid
    ]
    result, llm = investigate(s.full_script(critic=[critic_round({1: reqs})]))
    tasks = result.investigation.tasks
    critic = read_result(tasks["critic.r1"], CriticResult)

    assert tasks["critic.r1"].status is TaskStatus.SUCCEEDED and len(tasks["critic.r1"].attempts) == 1  # no retry
    assert critic.accepted_requests == ["ev.c1.1", "ev.c1.2"]
    assert tasks["ev.c1.1"].spec.inputs["tool"] == "search_logs"
    assert tasks["ev.c1.2"].spec.inputs["query"]["service"] == "fraud-check"
    reasons = [r.reason for r in critic.rejected_requests]
    assert reasons[0] == "unknown tool 'drop_tables'"
    assert reasons[1].startswith("invalid query:") and "end" in reasons[1]
    assert reasons[2] == "unknown hypothesis 'H9'"
    # Only registered tools were ever executed.
    executed = {t.spec.inputs["tool"] for t in tasks.values() if t.spec.kind == "evidence"}
    assert executed <= set(ALL_TOOLS)
    assert input_of(llm, "reasoning", ReasoningInput, call=1).new_evidence_refs == ["ev.c1.1", "ev.c1.2"]


def test_all_invalid_requests_with_revise_rerun_reasoning_without_new_evidence():
    reqs = [request("drop_tables", {}), request("query_metrics", "{not json")]
    result, llm = investigate(s.full_script(critic=[critic_round({1: reqs})], reasoning=[lenient_reasoning]))
    tasks = result.investigation.tasks
    critic = read_result(tasks["critic.r1"], CriticResult)

    assert critic.accepted_requests == [] and len(critic.rejected_requests) == 2
    assert critic.loop_decision == "revision_requested"
    assert not any(CRITIC_EVIDENCE.match(t) for t in tasks)
    assert input_of(llm, "reasoning", ReasoningInput, call=1).new_evidence_refs == []
    assert tasks["reasoning.r2"].status is TaskStatus.SUCCEEDED


def test_all_invalid_requests_with_insufficient_evidence_stop_the_loop():
    reqs = [request("drop_tables", {}), request("search_logs", {"start": "x", "end": "y"})]
    result, _ = investigate(s.full_script(critic=[critic_round({1: reqs}, verdict="insufficient_evidence")]))
    critic = read_result(result.investigation.tasks["critic.r1"], CriticResult)

    assert critic.loop_decision == "nothing_new_to_investigate"
    assert "reasoning.r2" not in result.investigation.tasks
    assert result.report.report.conclusion == "inconclusive"


def test_repeated_requests_are_rejected_within_and_across_rounds():
    same = request("search_logs", s.POOL_RELOAD_QUERY, "H1")
    script = s.full_script(critic=[critic_round({1: [same, same], 2: [same]})], reasoning=[lenient_reasoning])
    result, _ = investigate(script, config=AgentConfig(max_critic_rounds=3))
    tasks = result.investigation.tasks
    r1 = read_result(tasks["critic.r1"], CriticResult)
    r2 = read_result(tasks["critic.r2"], CriticResult)

    assert r1.accepted_requests == ["ev.c1.1"]
    assert [r.reason for r in r1.rejected_requests] == ["the same query was already run"]  # duplicate in one response
    assert r2.accepted_requests == []
    assert [r.reason for r in r2.rejected_requests] == ["the same query was already run"]  # repeat of round 1
    assert sorted(t for t in tasks if CRITIC_EVIDENCE.match(t)) == ["ev.c1.1"]


def test_tool_names_in_the_output_schema_match_the_registry():
    from app.agents.contracts import TOOL_NAMES

    assert set(TOOL_NAMES) == set(ALL_TOOLS)


def test_orchestrator_falls_back_to_default_scope_when_model_unavailable():
    down = LLMError(LLMErrorKind.UNAVAILABLE, "Could not connect to the model provider.")
    result, llm = investigate(s.full_script(orchestrator=[down]))
    env = read_envelope(result.investigation.tasks["orchestrate"])

    assert len(llm.calls_for("orchestrator")) == 2  # retried once, then fell back
    assert env.generated_by == "fallback"
    assert env.result["focus_services"] == ["payments-api"]


def test_reporting_falls_back_to_deterministic_inconclusive_report():
    down = LLMError(LLMErrorKind.TIMEOUT, "The model request timed out.")
    result, _ = investigate(s.full_script(reporting=[down]))

    assert result.report.generated_by == "fallback"
    assert result.report.report.conclusion == "inconclusive"
    assert {h.hypothesis_id for h in result.report.report.ranked_hypotheses} == {"H1", "H2"}
    assert result.report.report.facts  # observed facts carried over with their references


def test_deadline_still_yields_a_report():
    class Hangs:
        async def generate(self, request, output_type):
            await asyncio.Event().wait()

    config = AgentConfig(limits=EngineLimits(max_tasks=40, max_total_attempts=80, deadline_seconds=0.3))
    result, _ = investigate({}, llm=Hangs(), config=config)

    assert result.investigation.termination_reason is TerminationReason.DEADLINE_EXCEEDED
    assert result.investigation.tasks["report"].status is TaskStatus.SKIPPED
    assert result.report.generated_by == "fallback"
    assert result.report.report.conclusion == "inconclusive"


def test_secrets_in_unexpected_provider_exceptions_are_redacted():
    key = "sk-proj-THISISNOTAREALKEY1234567890"

    class Leaky:
        async def generate(self, request, output_type):
            raise RuntimeError(f"HTTP 401 Incorrect API key provided: {key}. Authorization: Bearer {key}")

    result, _ = investigate({}, llm=Leaky(), config=AgentConfig(max_critic_rounds=1))
    dumped = result.model_dump_json()

    assert result.investigation.tasks["orchestrate"].error.code is TaskErrorCode.HANDLER_ERROR
    assert key not in dumped
    assert "[REDACTED]" in result.investigation.tasks["orchestrate"].error.message


# --- Reference normalisation (Phase 3.2) -----------------------------------------------------------


def index_with_findings(*finding_refs):
    from app.agents.evidence import EvidenceItem, EvidenceSet, RefIndex
    from app.orchestration import EvidenceOutcome
    from app.schemas.evidence import EvidenceSource

    index = RefIndex().add_sets([
        EvidenceSet(ref="ev.logs", tool="search_logs", source=EvidenceSource.LOGS, outcome=EvidenceOutcome.COLLECTED,
                    query={}, warnings=[], error=None, items=[EvidenceItem(ref="ev.logs#0", record={})]),
    ])
    for ref in finding_refs:
        index.add_finding(ref, "fact", ["ev.logs#0"])
    return index


@pytest.mark.parametrize(
    ("ref", "expected", "candidates"),
    [
        ("dependency/dep-3", "dependency/dep-3", ["dependency/dep-3"]),  # canonical
        ("ev.logs#0", "ev.logs#0", ["ev.logs#0"]),  # canonical record
        ("dep-3", "dependency/dep-3", ["dependency/dep-3"]),  # unambiguous alias
        ("dep-9", None, []),  # unknown finding
        ("obs-1", None, []),  # finding never shown to this agent
        ("#0", None, []),  # record refs are never guessed
        ("0", None, []),
        ("logs#0", None, []),
        ("other/dep-3", None, []),  # wrong prefix is not rewritten
        ("x-1", None, ["dependency/x-1", "observability/x-1"]),  # ambiguous
    ],
)
def test_ref_index_resolves_only_unambiguous_shown_references(ref, expected, candidates):
    index = index_with_findings("dependency/dep-3", "dependency/x-1", "observability/x-1")

    assert index.resolve(ref) == (expected, candidates)


def test_ambiguous_alias_is_rejected_with_explanation():
    from app.agents.base import unknown_ref_problems

    index = index_with_findings("dependency/x-1", "observability/x-1")

    [problem] = unknown_ref_problems("H1", ["x-1"], index)
    assert "ambiguous reference 'x-1'" in problem and "dependency/x-1, observability/x-1" in problem
    assert unknown_ref_problems("H1", ["dep-9"], index) == ["H1 cites unknown reference 'dep-9'"]


def bare_finding_ids(request):
    out = s.reasoning(request)
    for h in out["hypotheses"]:
        h["supporting_refs"] = [r.split("/", 1)[1] if "/" in r else r for r in h["supporting_refs"]]
    return out


def test_unambiguous_short_references_are_normalised_without_a_retry():
    result, llm = investigate(s.full_script(reasoning=[bare_finding_ids], critic=[ACCEPT]))
    task = result.investigation.tasks["reasoning.r1"]
    env = read_envelope(task)

    assert task.status is TaskStatus.SUCCEEDED and len(task.attempts) == 1
    assert len(llm.calls_for("reasoning")) == 1
    h1 = next(h for h in env.result["output"]["hypotheses"] if h["hypothesis_id"] == "H1")
    assert h1["supporting_refs"] == ["dependency/dep-1", "observability/obs-1", "observability/obs-4"]
    notes = [a for a in env.adjustments if "normalised" in a]
    assert "hypotheses[0].supporting_refs: reference 'dep-1' normalised to 'dependency/dep-1'" in notes
    assert len(notes) == 5  # dep-1, obs-1, obs-4 (H1) + obs-2, dep-2 (H2)
    for ref in h1["supporting_refs"]:
        resolve(ref, result.investigation.tasks)  # provenance still traces to tool records


def test_unknown_short_reference_is_still_rejected():
    def invents(request):
        out = s.reasoning(request)
        out["hypotheses"][0]["supporting_refs"] = ["dep-99", "obs-1"]
        return out

    result, _ = investigate(s.full_script(reasoning=[invents, s.reasoning], critic=[ACCEPT]))
    task = result.investigation.tasks["reasoning.r1"]

    assert len(task.attempts) == 2
    assert task.attempts[0].error.code is TaskErrorCode.MODEL_INVALID_OUTPUT
    assert "unknown reference 'dep-99'" in task.attempts[0].error.message
    assert "obs-1" not in task.attempts[0].error.message  # the resolvable one was normalised, not reported


def test_short_reference_to_a_finding_not_shown_is_rejected():
    # Observability is only shown tool records, so a bare finding id cannot be resolved there.
    def cites_finding(request):
        out = s.observability(request)
        out["observations"][0]["evidence_refs"] = ["obs-2"]
        return out

    result, _ = investigate(s.full_script(observability=[cites_finding, s.observability], critic=[ACCEPT]))
    task = result.investigation.tasks["observability"]

    assert len(task.attempts) == 2
    assert "unknown reference 'obs-2'" in task.attempts[0].error.message


def test_prompt_requires_canonical_finding_references():
    from app.agents.base import SHARED_RULES

    assert '"dependency/dep-3", never a bare' in SHARED_RULES.replace("\n  ", " ")


# --- Timeout configuration (Phase 3.2) --------------------------------------------------------------


def test_default_timeouts_are_consistent(clean_env):
    from app.agents.orchestrator import AGENT_TIMEOUT_MARGIN_SECONDS
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    config = AgentConfig.from_settings(settings)

    assert settings.llm_timeout_seconds == 120
    assert config.agent_timeout_seconds == 120 + AGENT_TIMEOUT_MARGIN_SECONDS == AgentConfig().agent_timeout_seconds
    assert config.agent_timeout_seconds > settings.llm_timeout_seconds  # the SDK times out first
    assert config.limits.deadline_seconds == 300 >= config.agent_timeout_seconds
    assert config.agent_retry.max_attempts == 2  # retries stay bounded


def test_environment_override_reaches_sdk_engine_and_plan(clean_env, monkeypatch):
    from app.agents.contracts import ScopeOutput
    from app.agents.orchestrator import resolve_scope
    from app.core.config import Settings
    from app.llm import build_llm_client

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "200")
    settings = Settings(_env_file=None, llm_provider="openai", llm_model="m", llm_api_key="sk-test-not-a-real-key")
    config = AgentConfig.from_settings(settings)
    scope, _ = resolve_scope(ScopeOutput(**s.scope()), INC_2041, config)
    plan = OrchestratorAgent(llm=None, tools=build_evidence_tools(), config=config).plan_tasks(scope)

    assert settings.llm_timeout_seconds == 200
    assert build_llm_client(settings)._client.timeout == 200  # SDK per-request timeout; no request is made
    assert config.agent_timeout_seconds == 230
    assert {t.timeout_seconds for t in plan if t.kind.startswith("agent.")} == {230}


@pytest.mark.parametrize("value", ["0", "-5", "601", "abc"])
def test_invalid_timeout_values_are_rejected(clean_env, monkeypatch, value):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", value)
    with pytest.raises(ValidationError, match="llm_timeout_seconds"):
        Settings(_env_file=None)


def test_deadline_shorter_than_one_attempt_is_rejected(clean_env):
    from app.core.config import Settings

    settings = Settings(_env_file=None, llm_timeout_seconds=120, investigation_deadline_seconds=100)

    with pytest.raises(ValueError, match="INVESTIGATION_DEADLINE_SECONDS .* must be at least"):
        AgentConfig.from_settings(settings)
