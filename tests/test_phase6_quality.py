"""Phase 6: fixes for the defects seen in the one live EVAL-01 run, verified offline with scripted models.

The reproduction test makes the scripted model repeat the live run's mistakes on EVAL-01's real evidence
(a flat request rate cited for "increased load", a topology-only latency hypothesis, a critic still saying
"revise" at its limit, false "metrics unavailable" claims, a causal "fact" and no assumptions) and checks
that code now catches each one. Scripted outputs prove the mechanics, not real-model quality.
"""

import asyncio
import json
import pathlib

import pytest

from app.agents import AgentConfig, OrchestratorAgent
from app.agents.adjudication import adjudicate
from app.agents.base import read_result
from app.agents.contracts import (
    CriticOutput,
    CriticResult,
    CritiqueIssue,
    EvidenceInventory,
    Hypothesis,
    ReasoningResult,
    ReportOutput,
    Statement,
)
from app.agents.evidence import EvidenceClass, EvidenceItem, EvidenceSet, RefIndex
from app.agents.inventory import build_inventory, validate_availability_claims
from app.agents.report_rules import CODE_ADDED, finalize_report
from app.evaluation.dataset import load_cases
from app.llm import ScriptedLLMClient
from app.orchestration.models import EvidenceOutcome
from app.product.report_view import build_view, render_markdown
from app.schemas.evidence import EvidenceSource
from tests import agent_scripts as s

EVAL_01 = {c.case_id: c for c in load_cases()}["EVAL-01"]
T = "2026-09-01T{}:00Z"


# --- Small evidence worlds for unit tests ------------------------------------------------------------


def metric_set(ref, service, metric, values, unit="count"):
    times = [f"09:{m:02d}" for m in range(0, 60, 5)][: len(values)]
    return EvidenceSet(ref=ref, tool="query_metrics", source=EvidenceSource.METRICS, outcome=EvidenceOutcome.COLLECTED,
                       query={}, warnings=[], error=None, items=[
                           EvidenceItem(ref=f"{ref}#{i}", record={"timestamp": T.format(t), "service": service,
                                                                  "metric_name": metric, "value": v, "unit": unit})
                           for i, (t, v) in enumerate(zip(times, values))])


def record_set(ref, tool, source, records, outcome=EvidenceOutcome.COLLECTED, error_code=None, requested_by=None):
    return EvidenceSet(ref=ref, tool=tool, source=source, outcome=outcome, query={}, warnings=[],
                       error="boom" if error_code else None, error_code=error_code, requested_by=requested_by,
                       items=[EvidenceItem(ref=f"{ref}#{i}", record=r) for i, r in enumerate(records)])


LOG = {"timestamp": T.format("09:30"), "service": "payments-api", "level": "ERROR", "message": "Timed out waiting for DB connection"}
CHANGE = {"timestamp": T.format("09:25"), "change_id": "C1", "service": "payments-api", "change_type": "config_change",
          "status": "succeeded", "summary": "Reduce pool size", "details": {}}
EDGE = {"service": "payments-api", "depends_on": "fraud-check", "dependency_type": "external_api", "critical": True}
PAST = {"incident_id": "H-1", "title": "Old outage", "occurred_at": "2026-01-01T00:00:00Z", "services": ["a"]}


@pytest.fixture
def world():
    sets = [
        metric_set("m.rate", "payments-api", "request_rate_rps", [120, 121, 119, 122, 120, 123, 121, 120, 119], "rps"),
        metric_set("m.err", "payments-api", "error_rate_percent", [0.2, 0.2, 0.3, 0.2, 7.5, 7.6, 7.7, 7.4, 7.5], "percent"),
        record_set("l", "search_logs", EvidenceSource.LOGS, [LOG]),
        record_set("c", "get_change_history", EvidenceSource.CHANGES, [CHANGE]),
        record_set("d", "get_service_dependencies", EvidenceSource.DEPENDENCIES, [EDGE]),
        record_set("h", "search_historical_incidents", EvidenceSource.HISTORICAL_INCIDENTS, [PAST]),
        record_set("x", "search_logs", EvidenceSource.LOGS, [], outcome=EvidenceOutcome.FAILED, error_code="unavailable"),
        record_set("n", "search_logs", EvidenceSource.LOGS, [], outcome=EvidenceOutcome.NO_MATCHES),
    ]
    refs = RefIndex().add_sets(sets)
    refs.add_finding("dependency/dep-1", "fact", ["d#0"])
    refs.add_finding("observability/obs-1", "fact", ["m.err#6"])
    refs.add_finding("observability/obs-2", "interpretation", ["l#0"])
    return sets, refs


# --- Evidence classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("m.rate#8", EvidenceClass.NO_CHANGE),  # flat request rate
        ("m.err#6", EvidenceClass.DIRECT),  # rising error rate
        ("m.err#0", EvidenceClass.NO_CHANGE),  # a baseline sample
        ("l#0", EvidenceClass.DIRECT),
        ("c#0", EvidenceClass.DIRECT),
        ("d#0", EvidenceClass.CONTEXTUAL),  # topology is a relationship, not a failure
        ("h#0", EvidenceClass.CONTEXTUAL),
        ("x", EvidenceClass.MISSING),
        ("n", EvidenceClass.CONTEXTUAL),
        ("dependency/dep-1", EvidenceClass.CONTEXTUAL),  # a "fact" finding inherits its records' class
        ("observability/obs-1", EvidenceClass.DIRECT),
        ("observability/obs-2", EvidenceClass.CONTEXTUAL),  # interpretation
        ("nope#1", EvidenceClass.UNKNOWN),
    ],
)
def test_evidence_classification(world, ref, expected):
    _, refs = world
    assert refs.classify(ref).evidence_class is expected


def test_flat_metric_reason_states_the_values(world):
    _, refs = world
    reason = refs.classify("m.rate#8").reason
    assert "request_rate_rps = 119 vs baseline 120" in reason and "within the normal range" in reason


# --- Evidence inventory and availability claims --------------------------------------------------------


def test_inventory_statuses_distinguish_no_matches_failure_timeout_and_not_run():
    sets = [
        metric_set("ev.metrics", "payments-api", "request_rate_rps", [1, 1, 1], "rps"),
        record_set("ev.nm", "search_logs", EvidenceSource.LOGS, [], outcome=EvidenceOutcome.NO_MATCHES),
        record_set("ev.f", "get_change_history", None, [], outcome=EvidenceOutcome.FAILED, error_code="unavailable"),
        record_set("ev.t", "search_logs", None, [], outcome=EvidenceOutcome.FAILED, error_code="timeout"),
        record_set("ev.c1.1", "query_metrics", None, [], outcome=EvidenceOutcome.NOT_COLLECTED, requested_by="critic.r1"),
    ]
    inv = build_inventory(sets)

    assert {e.task_id: e.status for e in inv.entries} == {
        "ev.metrics": "retrieved", "ev.nm": "no_matches", "ev.f": "failed", "ev.t": "timed_out", "ev.c1.1": "not_run"}
    assert inv.retrieved_signals == ["metrics: payments-api/request_rate_rps"]
    assert inv.never_queried_tools == ["get_service_dependencies", "search_historical_incidents"]
    assert not any("ev.nm" in u for u in inv.unavailable)  # a query that ran and matched nothing is not a gap
    assert any(u.startswith("ev.t (search_logs) timed out") and "not proof the signal is absent" in u for u in inv.unavailable)
    assert next(e for e in inv.entries if e.task_id == "ev.c1.1").requested_by == "critic.r1"


def test_false_unavailability_claims_are_removed_and_true_ones_kept():
    inv = EvidenceInventory(entries=[], unavailable=[], never_queried_tools=[], retrieved_signals=[
        "metrics: payments-api/request_rate_rps", "metrics: postgres-payments/cpu_percent", "logs: payments-api"])
    claims = [
        "Metrics for request rates leading into the incident were not available.",  # live claim: false
        "Performance metrics for the postgres database during peak events were missing.",  # live claim: false
        "payments-api logs were not retrieved.",  # false
        "fraud-check latency metrics were not available.",  # true gap
        "Change history for fraud-check is not visible.",  # true gap
        "The critic round limit was reached.",  # not an availability claim
    ]
    kept, removed = validate_availability_claims(claims, inv)

    assert kept == claims[3:]
    assert len(removed) == 3 and "request_rate_rps" in removed[0] and "postgres-payments/cpu_percent" in removed[1]


# --- Adjudication ----------------------------------------------------------------------------------------


def hyp(hid, confidence, support, against=(), title=None):
    return Hypothesis(hypothesis_id=hid, title=title or hid, description="d", supporting_refs=list(support),
                      contradicting_refs=list(against), missing_information=[], confidence=confidence,
                      confidence_rationale="r")


def critique(verdict="revise", issues=(), accepted=(), round_no=2):
    return CriticResult(round=round_no, reviewed_task=f"reasoning.r{round_no}",
                        output=CriticOutput(verdict=verdict, issues=list(issues), evidence_requests=[], summary="s"),
                        final_verdict=verdict, loop_decision="max_rounds_reached", accepted_requests=list(accepted),
                        rejected_requests=[])


def decide(world, hypotheses, crit=None, sets_extra=(), critic_expected=True, insufficient=False):
    sets, refs = world
    inv = build_inventory([*sets, *sets_extra])
    return adjudicate(hypotheses, insufficient, refs, inv, final_critique=crit, critiques=[crit] if crit else [],
                      critic_expected=critic_expected)


def test_strong_direct_evidence_without_contradiction_is_accepted_despite_revise(world):
    issue = CritiqueIssue(issue_id="C1", hypothesis_id="H1", issue_type="missing_evidence", detail="want more", evidence_refs=[])
    a = decide(world, [hyp("H1", "medium", ["c#0", "l#0"]), hyp("H2", "low", ["d#0"])], critique(issues=[issue]))

    assert a.decision == "cause_accepted" and a.leading_hypothesis == "H1"
    assert a.basis == "critic verdict 'revise' adjudicated on evidence sufficiency"
    assert a.unresolved_critic_issues == ["C1 (missing_evidence, H1): want more"]


def test_credible_competing_cause_keeps_it_inconclusive(world):
    a = decide(world, [hyp("H1", "medium", ["c#0", "l#0"]), hyp("H2", "low", ["observability/obs-1", "l#0"])], critique())

    assert a.decision == "inconclusive"
    assert any(b.startswith("no_credible_alternative: H2") for b in a.blocking_issues)


def test_direct_contradiction_as_broad_as_support_blocks(world):
    a = decide(world, [hyp("H1", "medium", ["c#0"], against=["l#0"])], critique())
    weaker = decide(world, [hyp("H1", "medium", ["c#0", "observability/obs-1"], against=["l#0"])], critique())

    assert a.decision == "inconclusive"
    assert any(b.startswith("no_material_contradiction") for b in a.blocking_issues)
    contradiction = next(c for c in weaker.checks if c.name == "no_material_contradiction")
    assert contradiction.passed and "weaker contradicting evidence retained" in contradiction.detail


def test_critic_contradiction_citing_direct_evidence_blocks(world):
    issue = CritiqueIssue(issue_id="C1", hypothesis_id="H1", issue_type="contradiction", detail="errors before change",
                          evidence_refs=["l#0"])
    a = decide(world, [hyp("H1", "medium", ["c#0", "observability/obs-1"])], critique(issues=[issue]))

    assert a.decision == "inconclusive" and "C1 (contradiction) citing l#0" in a.blocking_issues[0]


def test_failed_follow_up_evidence_blocks(world):
    failed = record_set("ev.c1.1", "query_metrics", None, [], outcome=EvidenceOutcome.FAILED, error_code="unavailable")
    a = decide(world, [hyp("H1", "medium", ["c#0", "l#0"])], critique(accepted=["ev.c1.1"]), sets_extra=[failed])

    assert a.decision == "inconclusive"
    assert any("follow_ups_obtained" in b and "ev.c1.1 failed" in b for b in a.blocking_issues)


@pytest.mark.parametrize(
    ("hypotheses", "crit", "insufficient", "failing_check"),
    [
        ([hyp("H1", "medium", ["d#0", "h#0"])], "revise", False, "direct_support"),  # topology + history only
        ([hyp("H1", "medium", ["c#0"])], "accept", False, "direct_support"),  # one source, even if critic accepts
        ([hyp("H1", "medium", ["c#0", "l#0"]), hyp("H2", "medium", ["d#0"])], "accept", False, "leader_clearly_ahead"),
        ([hyp("H1", "low", ["c#0", "l#0"])], "accept", False, "leader_confident"),
        ([hyp("H1", "medium", ["c#0", "l#0"])], None, False, "review_completed"),
        ([hyp("H1", "medium", ["c#0", "l#0"])], "accept", True, "hypotheses_available"),
        ([], "accept", False, "hypotheses_available"),
    ],
)
def test_inconclusive_with_the_exact_blocking_reason(world, hypotheses, crit, insufficient, failing_check):
    a = decide(world, hypotheses, critique(crit) if crit else None, insufficient=insufficient)

    assert a.decision == "inconclusive"
    assert any(b.startswith(failing_check) for b in a.blocking_issues), a.blocking_issues


def test_single_agent_needs_no_critic(world):
    a = decide(world, [hyp("H1", "medium", ["c#0", "l#0"])], critic_expected=False)

    assert a.decision == "cause_accepted" and a.basis.startswith("evidence sufficiency (single agent")


# --- Report rules -----------------------------------------------------------------------------------------


def report(conclusion="inconclusive", facts=(), assumptions=(), limitations=()):
    return ReportOutput(conclusion=conclusion, headline="h", summary="s", ranked_hypotheses=[], facts=list(facts),
                        interpretations=[], assumptions=list(assumptions), would_confirm=[], would_disconfirm=[],
                        limitations=list(limitations), corrective_actions=[], open_questions=[])


def test_facts_assumptions_and_interpretations_are_separated(world):
    sets, refs = world
    leader = hyp("H1", "medium", ["c#0", "l#0"], title="Pool size reduction")
    adj = decide(world, [leader], critic_expected=False)
    out, notes = finalize_report(
        report(facts=[Statement(text="Error rate peaked at 7.7%.", evidence_refs=["m.err#6"]),
                      Statement(text="Pool saturation caused the timeouts.", evidence_refs=["l#0"]),
                      Statement(text="Request rate increased sharply.", evidence_refs=["m.rate#8"]),
                      Statement(text="An uncited claim.", evidence_refs=[])]),
        hypotheses=[leader], refs=refs, inventory=build_inventory(sets), adjudication=adj, known_limitations=[])

    assert [f.text for f in out.facts] == ["Error rate peaked at 7.7%."]
    assert [i.text for i in out.interpretations] == ["Pool saturation caused the timeouts.", "Request rate increased sharply."]
    assert "An uncited claim." in out.assumptions
    assert out.conclusion == "probable_cause_identified"


def test_material_assumption_is_added_when_the_model_lists_none(world):
    sets, refs = world
    leader = hyp("H1", "medium", ["c#0", "l#0"], title="Pool size reduction")
    adj = decide(world, [leader], critic_expected=False)
    with_h, _ = finalize_report(report(), hypotheses=[leader], refs=refs, inventory=build_inventory(sets),
                                adjudication=adj, known_limitations=[])
    without_h, _ = finalize_report(report(), hypotheses=[], refs=refs, inventory=build_inventory(sets),
                                   adjudication=decide(world, [], critic_expected=False), known_limitations=[])

    assert with_h.assumptions[0].startswith(CODE_ADDED + "Pool size reduction: the causal link is inferred")
    assert without_h.assumptions == []  # no causal hypothesis, so no premise is assumed


def test_deterministic_reports_never_accept_a_cause(world):
    sets, refs = world
    leader = hyp("H1", "medium", ["c#0", "l#0"])
    out, _ = finalize_report(report(), hypotheses=[leader], refs=refs, inventory=build_inventory(sets),
                             adjudication=decide(world, [leader], critic_expected=False), known_limitations=[],
                             allow_accept=False)

    assert out.conclusion == "inconclusive"


# --- Dataset bound ---------------------------------------------------------------------------------------------


def test_percentages_never_exceed_100_in_any_dataset():
    files = [*pathlib.Path("data/eval/cases").glob("*/metrics.jsonl"), pathlib.Path("data/fixtures/metrics.jsonl")]
    values = [json.loads(line) for f in files for line in f.read_text().splitlines()]
    percents = [r["value"] for r in values if r["unit"] == "percent"]

    assert percents and all(0 <= v <= 100 for v in percents)
    pool = [r["value"] for r in values if r["metric_name"] == "db_pool_utilization_percent" and "2026-09-01" in r["timestamp"]]
    assert max(pool) == 100.0 and min(pool) < 40  # the causal signal is preserved


# --- Reproduction of the live EVAL-01 defects -------------------------------------------------------------------


def live_like_script():
    """Scripted responses that repeat the live gpt-4o-mini run's mistakes on EVAL-01's real evidence."""

    def observability(req):
        d = s.payload(req)
        sat = s.refs_where(d, s.message_has("pool saturated"))
        err = [s.summary_ref(d, "payments-api", "error_rate_percent", k) for k in ("peak_ref", "last_ref")]
        return {"observations": [
            {"observation_id": "obs-1", "kind": "fact", "statement": "Connection pool saturated (8/8 in use).",
             "services": ["payments-api"], "evidence_refs": sat},
            {"observation_id": "obs-2", "kind": "fact", "statement": "Error rate peaked during the incident.",
             "services": ["payments-api"], "evidence_refs": err},
            {"observation_id": "obs-3", "kind": "fact", "statement": "504s from api-gateway.",
             "services": ["api-gateway"], "evidence_refs": s.refs_where(d, s.message_has("504"))},
        ], "onset_estimate": None, "evidence_gaps": [], "summary": "s"}

    def dependency(req):
        d = s.payload(req)
        change = [c["change_ref"] for c in d["change_relations"] if c["change_id"] == "CHG-E01-2"]
        edge = s.refs_where(d, lambda r: r.get("depends_on") == "fraud-check")
        return {"findings": [
            {"finding_id": "dep-1", "kind": "fact", "statement": "Pool size cut 40 -> 8 at 09:55.", "services": ["payments-api"], "evidence_refs": change},
            {"finding_id": "dep-3", "kind": "fact", "statement": "payments-api depends on fraud-check.", "services": ["payments-api"], "evidence_refs": edge},
        ], "evidence_gaps": [], "summary": "s"}

    def reasoning(req):
        d = s.payload(req)
        flat_rate = s.refs_where(d, lambda r: r.get("metric_name") == "request_rate_rps" and r["timestamp"].endswith("10:15:00Z"))[:1]
        return {"hypotheses": [
            {"hypothesis_id": "H1", "title": "Connection pool saturation due to reduced pool size", "description": "d",
             "supporting_refs": ["dependency/dep-1", "observability/obs-1"], "contradicting_refs": [],
             "missing_information": ["Pool wait-time histogram"], "confidence": "high", "confidence_rationale": "r"},
            {"hypothesis_id": "H2", "title": "External dependency latency (fraud-check)", "description": "fraud-check slow",
             "supporting_refs": ["dependency/dep-3"], "contradicting_refs": ["observability/obs-3"],
             "missing_information": [], "confidence": "medium", "confidence_rationale": "r"},
            {"hypothesis_id": "H3", "title": "Increased load on payments-api", "description": "traffic increase",
             "supporting_refs": ["observability/obs-2", *flat_rate], "contradicting_refs": [],
             "missing_information": [], "confidence": "medium", "confidence_rationale": "r"},
        ], "insufficient_evidence": False, "critique_responses": ["Addressed."] if d["critique"] else [], "summary": "s"}

    def critic(req):
        d = s.payload(req)
        rate = {"start": "2026-09-01T10:00:00Z", "end": "2026-09-01T10:15:00Z", "services": ["payments-api"],
                "metric_names": ["request_rate_rps"]}
        return {"verdict": "revise", "summary": "Still unsure.",
                "issues": [{"issue_id": "C1", "hypothesis_id": "H3", "issue_type": "missing_evidence",
                            "detail": "Need traffic data.", "evidence_refs": []}],
                "evidence_requests": [{"tool": "query_metrics", "query_json": json.dumps(rate), "reason": "load?",
                                       "hypothesis_id": "H3"}] if d["round"] == 1 else []}

    def reporting(req):
        d = s.payload(req)
        hyps = d["final_reasoning"]["output"]["hypotheses"]
        return {"conclusion": "inconclusive", "headline": "Investigation into card payment failures", "summary": "s",
                "ranked_hypotheses": [{"rank": i, "hypothesis_id": h["hypothesis_id"], "title": h["title"],
                                       "confidence": h["confidence"], "supporting_refs": h["supporting_refs"],
                                       "contradicting_refs": h["contradicting_refs"], "assessment": h["description"]}
                                      for i, h in enumerate(hyps, start=1)],
                "facts": [{"text": "Pool utilisation peaked, indicating a saturated pool causing timeouts.",
                           "evidence_refs": ["observability/obs-1"]}],
                "interpretations": [], "assumptions": [], "would_confirm": [], "would_disconfirm": [],
                "limitations": ["Metrics for request rates leading into the incident were not available.",
                                "Performance metrics for the postgres database during peak events were missing."],
                "corrective_actions": [{"action": "Restore pool size to 40.", "timing": "immediate", "hypothesis_id": "H1"}],
                "open_questions": []}

    return {"orchestrator": [s.scope({"start": "2026-09-01T09:00:00Z", "end": "2026-09-01T10:45:00Z"})],
            "observability": [observability], "dependency": [dependency], "reasoning": [reasoning],
            "critic": [critic], "reporting": [reporting]}


@pytest.fixture(scope="module")
def live_like():
    llm = ScriptedLLMClient(live_like_script())
    return asyncio.run(OrchestratorAgent(llm, EVAL_01.tools(), AgentConfig()).investigate(EVAL_01.incident, "inv-live-like"))


def test_reproduction_flat_rate_is_not_support_for_increased_load(live_like):
    r2 = read_result(live_like.investigation.tasks["reasoning.r2"], ReasoningResult)
    h3 = next(h for h in r2.output.hypotheses if h.hypothesis_id == "H3")
    moved = [c for c in r2.citation_checks if c.hypothesis_id == "H3"]

    assert moved and moved[0].action == "moved_to_contradicting" and "within the normal range" in moved[0].detail
    assert moved[0].ref in h3.contradicting_refs and moved[0].ref not in h3.supporting_refs


def test_reproduction_topology_only_hypothesis_is_capped(live_like):
    r2 = read_result(live_like.investigation.tasks["reasoning.r2"], ReasoningResult)
    h2 = next(h for h in r2.output.hypotheses if h.hypothesis_id == "H2")
    critic = read_result(live_like.investigation.tasks["critic.r2"], CriticResult)

    assert h2.confidence == "low"
    assert any(i.hypothesis_id == "H2" and "relationship, not that anything failed" in i.detail for i in critic.output.issues)


def test_reproduction_correct_cause_is_accepted_on_evidence(live_like):
    final = live_like.report
    a = final.adjudication

    assert final.final_critic_verdict == "revise"  # the critic never accepted, as in the live run
    assert a.decision == "cause_accepted" and a.leading_hypothesis == "H1" and all(c.passed for c in a.checks)
    assert final.report.conclusion == "probable_cause_identified"
    assert final.report.ranked_hypotheses[0].hypothesis_id == "H1"
    assert any("adjudication" in n and "proposed inconclusive" in n for n in final.adjustments)


def test_reproduction_false_availability_claims_are_removed(live_like):
    final = live_like.report
    signals = final.evidence_inventory.retrieved_signals

    assert "metrics: payments-api/request_rate_rps" in signals and "metrics: postgres-payments/cpu_percent" in signals
    assert not any("request rate" in lim or "postgres" in lim for lim in final.report.limitations)
    assert sum("Removed claim" in n for n in final.adjustments) == 2


def test_reproduction_facts_interpretations_and_assumptions(live_like):
    out = live_like.report.report

    assert out.facts == []
    assert [i.text for i in out.interpretations] == ["Pool utilisation peaked, indicating a saturated pool causing timeouts."]
    assert out.assumptions and out.assumptions[0].startswith(CODE_ADDED)
    assert out.would_confirm == [f"{CODE_ADDED}Pool wait-time histogram"]


def test_reproduction_report_view_shows_classes_and_decision(live_like):
    view = build_view(live_like, evidence_source="EVAL-01", model="scripted")
    md = render_markdown(view)

    assert view.unresolved_citations == []
    h3 = next(h for h in view.hypotheses if h.hypothesis_id == "H3")
    assert any(c.evidence_class == "no_change" for c in h3.contradicting)
    assert "## Decision (adjudication)" in md and "PASS `direct_support`" in md
    assert "## Evidence availability" in md and "request_rate_rps" in md
    assert "## Interpretations" in md and "## Assumptions (unverified premises)" in md
