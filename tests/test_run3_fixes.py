"""Regressions from live run 3 of EVAL-01 (artifacts/live-runs/EVAL-01-run3).

1. The accepted top hypothesis was titled "Database Connection Pool Saturation": the mechanism, not the cause. It
   cited the config change that cut the pool from 40 to 8, but its title never named it, so the answer key scored it
   as a wrong conclusion. A top hypothesis that rests on a change record now names that change.
2. "Increased Load on Payments API" was supported only by a latency increase while the request rate was flat.
   Symptom metrics no longer support a load claim when the request rate shows no change.
"""

import pytest

from app.agents.contracts import Hypothesis, ReportOutput, Statement
from app.agents.evidence import EvidenceClass
from app.agents.grounding import check_citations
from app.agents.inventory import build_inventory
from app.agents.report_rules import finalize_report
from app.evaluation.dataset import load_labels
from app.evaluation.metrics import matched_label
from app.evaluation.normalize import NormalizedHypothesis
from app.schemas.evidence import EvidenceSource
from tests.test_metric_baseline import eval01_index, evidence
from tests.test_phase6_quality import decide, hyp, world  # noqa: F401  (world is a fixture)


def report_with(*hypotheses: Hypothesis) -> ReportOutput:
    from app.agents.contracts import RankedHypothesis

    ranked = [RankedHypothesis(rank=i, hypothesis_id=h.hypothesis_id, title=h.title, confidence=h.confidence,
                               supporting_refs=h.supporting_refs, contradicting_refs=h.contradicting_refs,
                               assessment="Strong evidence shows connection pool saturation.")
              for i, h in enumerate(hypotheses, start=1)]
    return ReportOutput(conclusion="probable_cause_identified", headline="h", summary="s", ranked_hypotheses=ranked,
                        facts=[], interpretations=[], assumptions=[], would_confirm=[], would_disconfirm=[],
                        limitations=[], corrective_actions=[], open_questions=[])


def finalize(world, h: Hypothesis, timelines=()):
    sets, refs = world
    return finalize_report(report_with(h), hypotheses=[h], refs=refs, inventory=build_inventory(sets),
                           adjudication=decide(world, [h], critic_expected=False), known_limitations=[],
                           timelines=list(timelines))


# --- 1. The top hypothesis names the change it rests on ---------------------------------------------------------


def test_top_hypothesis_resting_on_a_change_names_it(world):
    out, notes = finalize(world, hyp("H1", "medium", ["c#0", "l#0"], title="Database Connection Pool Saturation"))

    title = out.ranked_hypotheses[0].title
    assert title.startswith("Database Connection Pool Saturation") and "C1" in title and "Reduce pool size" in title
    assert any("names the change it cites" in n for n in notes)


@pytest.mark.parametrize("title", ["Pool cut by C1", "Reduce pool size (payments-api config_change)"])
def test_title_already_naming_the_change_by_id_or_summary_is_left_alone(world, title):
    out, notes = finalize(world, hyp("H1", "medium", ["c#0", "l#0"], title=title))

    assert out.ranked_hypotheses[0].title == title and not any("names the change" in n for n in notes)


def test_hypothesis_without_a_cited_change_is_left_alone(world):
    out, _ = finalize(world, hyp("H1", "medium", ["l#0", "m.err#6"], title="Errors: DB timeouts"))

    assert out.ranked_hypotheses[0].title == "Errors: DB timeouts"


def test_change_ruled_out_by_the_timeline_is_not_named(world):
    from app.agents.contracts import ChangeTiming, TemporalAssessment

    blocked = TemporalAssessment(
        hypothesis_id="H1", status="change_follows_onset", onset="2026-09-01T09:20:00Z", onset_refs=["l#0"],
        onset_precision_seconds=0, blocks_cause=True, may_have_worsened=False, worsening_refs=[], detail="d",
        changes=[ChangeTiming(change_ref="c#0", change_id="C1", change_type="config_change", change_status="succeeded",
                              service="payments-api", time="2026-09-01T09:25:00Z", relation="follows_onset", detail="d",
                              rollback_ref=None, persisted_refs=[])])
    out, _ = finalize(world, hyp("H1", "medium", ["c#0", "l#0"], title="Pool saturation"), timelines=[blocked])

    assert out.ranked_hypotheses[0].title == "Pool saturation"


def test_run3_top_hypothesis_now_matches_the_answer_key_on_eval01_evidence():
    """The real EVAL-01 change record (pool 40 -> 8) and run 3's own title and assessment."""
    index = eval01_index("2026-09-01T09:00:00Z")
    tools = __import__("tests.test_metric_baseline", fromlist=["EVAL_01"]).EVAL_01.tools()
    change = [r.model_dump(mode="json") for r in tools["get_change_history"].run(
        {"start": "2026-09-01T00:00:00Z", "end": "2026-09-01T12:00:00Z"}).records]
    index.add_sets([evidence("ev.changes", EvidenceSource.CHANGES, change)])
    ref = next(f"ev.changes#{i}" for i, r in enumerate(change) if r["change_id"] == "CHG-E01-2")
    pool = next(r for r, src, rec in index.records() if src is EvidenceSource.METRICS
                and rec["metric_name"] == "db_pool_utilization_percent" and rec["timestamp"] == "2026-09-01T10:00:00Z")
    h1 = hyp("H1", "high", [ref, pool], title="Database Connection Pool Saturation")
    sets = []  # inventory content is irrelevant here
    out, _ = finalize_report(report_with(h1), hypotheses=[h1], refs=index, inventory=build_inventory(sets),
                             adjudication=decide(([], index), [h1], critic_expected=False), known_limitations=[])

    top = out.ranked_hypotheses[0]
    assert "CHG-E01-2" in top.title and "40 → 8" in top.title
    normalized = NormalizedHypothesis(rank=1, hypothesis_id="H1", title=top.title, assessment=top.assessment,
                                      confidence=top.confidence, supporting_refs=top.supporting_refs,
                                      contradicting_refs=top.contradicting_refs)
    assert matched_label(normalized, load_labels()["EVAL-01"]) is not None  # the answer key itself is unchanged


# --- 2. Latency alone does not support "increased load" when the request rate is flat ---------------------------


def eval01_ref(index, metric: str, at: str) -> str:
    return next(r for r, src, rec in index.records() if src is EvidenceSource.METRICS and rec["metric_name"] == metric
                and rec["timestamp"] == at)


def test_latency_alone_does_not_support_increased_load_when_request_rate_is_flat():
    index = eval01_index("2026-09-01T09:00:00Z")
    latency = eval01_ref(index, "latency_p95_ms", "2026-09-01T10:00:00Z")
    assert index.classify(latency).evidence_class is EvidenceClass.DIRECT

    support, against, checks = check_citations("H2", "Increased Load on Payments API", [latency], [], index)

    assert support == []
    flat = [r for r in against if index.record(r)[1]["metric_name"] == "request_rate_rps"]
    assert flat and index.classify(flat[0]).evidence_class is EvidenceClass.NO_CHANGE
    assert {c.action for c in checks} == {"removed_from_support", "added_to_contradicting"}
    assert any("request rate" in c.detail for c in checks)


def test_latency_still_supports_a_claim_that_is_not_about_load():
    index = eval01_index("2026-09-01T09:00:00Z")
    latency = eval01_ref(index, "latency_p95_ms", "2026-09-01T10:00:00Z")

    support, against, checks = check_citations("H1", "Database connection pool saturation", [latency], [], index)

    assert support == [latency] and against == [] and checks == []


def series(name, values):
    return [{"service": "svc", "metric_name": name, "timestamp": f"2026-09-01T09:{m:02d}:00Z", "value": v, "unit": "u"}
            for m, v in zip(range(0, 5 * len(values), 5), values)]


@pytest.mark.parametrize(("rate", "kept"), [
    ([100, 101, 99, 100, 100, 100, 99, 400, 410, 420], True),  # the request rate really rose: latency may support load
    (None, True),                                  # no request-rate data: flatness is not shown, so no change
])
def test_load_claim_is_only_checked_against_a_request_rate_that_is_shown_flat(rate, kept):
    from app.agents.evidence import RefIndex

    records = series("latency_ms", [100, 101, 99, 100, 100, 100, 99, 900, 950, 960])
    if rate:
        records += series("request_rate_rps", rate)
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, records, reported_at=None)])
    latency = "m#9"
    assert index.classify(latency).evidence_class is EvidenceClass.DIRECT

    support, _, _ = check_citations("H2", "Traffic surge overloaded the service", [latency], [], index)

    assert (support == [latency]) is kept


def test_report_statement_type_is_unchanged():
    assert Statement(text="t", evidence_refs=[]).text == "t"


# --- 3. One boundary sample in the baseline must not inflate the noise floor (live run 4) -------------------------


def run4_index():
    """Rebuild live run 4's evidence from its recorded queries (artifacts/live-runs/EVAL-01-run4)."""
    import json

    from app.agents.evidence import EvidenceItem, EvidenceSet, RefIndex
    from app.orchestration.models import EvidenceOutcome
    from tests.test_metric_baseline import EVAL_01

    report = json.load(open("artifacts/live-runs/EVAL-01-run4/live_report.json"))
    sources = {"query_metrics": EvidenceSource.METRICS, "search_logs": EvidenceSource.LOGS,
               "get_change_history": EvidenceSource.CHANGES}
    tools, sets = EVAL_01.tools(), []
    for e in report["evidence_inventory"]["entries"]:
        if e["tool"] in sources:
            records = [r.model_dump(mode="json") for r in tools[e["tool"]].run(e["query"]).records]
            sets.append(EvidenceSet(ref=e["task_id"], tool=e["tool"], source=sources[e["tool"]],
                                    outcome=EvidenceOutcome.COLLECTED, query={}, warnings=[], error=None,
                                    items=[EvidenceItem(ref=f"{e['task_id']}#{i}", record=r) for i, r in enumerate(records)],
                                    incident_reported_at="2026-09-01T10:12:00Z", incident_affected_services=["payments-api"]))
    return RefIndex().add_sets(sets)


def test_run4_error_rate_jump_is_a_change_not_noise():
    index = run4_index()
    ref = next(r for r, src, rec in index.records() if src is EvidenceSource.METRICS and rec["metric_name"] ==
               "error_rate_percent" and rec["timestamp"] == "2026-09-01T10:00:00Z")

    c = index.classify(ref)

    assert c.evidence_class is EvidenceClass.DIRECT, c.reason  # run 4 reported "+3705%: within the normal range"
    assert "7.61 vs baseline 0.2" in c.reason


def test_quantised_baseline_still_counts_its_step_as_noise():
    from app.agents.evidence import RefIndex

    values = [0.2, 0.3, 0.2, 0.3, 0.2, 0.3, 0.2, 0.3, 0.2, 0.3]  # alternates by one step
    records = series("error_rate_percent", values + [0.3])
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, records, reported_at=None)])

    assert index.classify("m#10").evidence_class is EvidenceClass.NO_CHANGE
