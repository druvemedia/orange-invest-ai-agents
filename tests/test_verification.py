"""Evidence-to-hypothesis verification, tested on live run 4's real evidence and on counterexamples.

Run 4 of EVAL-01 (artifacts/live-runs/EVAL-01-run4) titled its leading hypothesis "DB Connection Pool Size Reduced
from 40 to 8 by CHG-E01-2", but cited the CHG-E01-2 record only through an interpretation, and cited the timeout
logs ("Timed out waiting for DB connection from pool") against it. Each rule below must fix that pattern without
overturning genuine contradictions.
"""

import json

import pytest

from app.agents.evidence import EvidenceClass, EvidenceItem, EvidenceSet, RefIndex
from app.agents.grounding import assess_citations, check_citations, named_changes
from app.orchestration.models import EvidenceOutcome
from app.schemas.evidence import EvidenceSource
from tests.test_run3_fixes import run4_index

RUN4 = json.load(open("artifacts/live-runs/EVAL-01-run4/live_report.json"))


def run4_refs() -> RefIndex:
    """Run 4's evidence (rebuilt from its recorded queries) plus the findings its hypotheses cite."""
    index = run4_index()
    for h in RUN4["hypotheses"]:
        for c in h["supporting"] + h["contradicting"]:
            if c["kind"] == "finding":
                kind = "fact" if c["text"].startswith("[fact]") else "interpretation"
                index.add_finding(c["ref"], kind, [b["ref"] for b in c.get("based_on") or []])
    return index


def run4_h1():
    h = sorted(RUN4["hypotheses"], key=lambda h: h["rank"])[0]
    return h, f"{h['title']} {h['assessment']}", [c["ref"] for c in h["supporting"]], [c["ref"] for c in h["contradicting"]]


def change_ref(index: RefIndex, change_id: str) -> str:
    return next(ref for ref, src, r in index.records() if src is EvidenceSource.CHANGES and r["change_id"] == change_id)


# --- Run 4: the named change ----------------------------------------------------------------------------------------


def test_run4_hypothesis_naming_chg_e01_2_now_cites_its_change_record():
    index = run4_refs()
    _, claim, support, against = run4_h1()
    record = change_ref(index, "CHG-E01-2")
    assert record not in index.direct_leaves(support)  # run 4 reached it only through an interpretation

    kept, _, checks = check_citations("H1", claim, support, against, index)

    assert record in kept and index.classify(record).evidence_class is EvidenceClass.DIRECT
    added = [c for c in checks if c.action == "added_to_support"]
    assert [c.ref for c in added] == [record] and "not that it caused anything" in added[0].detail


def test_negated_change_is_not_added():
    index = run4_refs()

    assert named_changes("Not caused by CHG-E01-2; the timeouts began independently", index) == {}
    assert list(named_changes("Pool cut by CHG-E01-2", index)) == ["CHG-E01-2"]


def test_a_named_change_after_onset_is_added_but_the_timeline_still_blocks_it():
    from app.agents.temporal import assess
    from tests.test_phase7_temporal_independence import change, hyp, log, t, world

    _, refs = world(logs=[log("09:30"), log("09:35")], changes=[change("CHG-701", t("09:50"))])
    kept, _, _ = check_citations("H1", "Outage caused by CHG-701", ["l#0"], [], refs)

    assert "c#0" in kept  # a candidate cause, cited...
    a = assess(hyp(kept), refs, ["api"])
    assert a.status == "change_follows_onset" and a.blocks_cause  # ...but not a proven one


# --- Run 4: failure logs cited against their own mechanism -------------------------------------------------------


def test_run4_pool_timeout_logs_are_not_counted_against_the_pool_hypothesis():
    index = run4_refs()
    _, claim, support, against = run4_h1()

    kept, still_against, checks = check_citations("H1", claim, support, against, index)

    set_aside = {c.ref for c in checks if c.action == "contradiction_set_aside"}
    assert "observability/obs-1" in set_aside  # "Timed out waiting for DB connection from pool" (10:02)
    assert "observability/obs-1" not in still_against and "observability/obs-1" not in kept  # neither side
    for ref in still_against:  # whatever remains is kept for a reason the rule cannot dismiss
        assert ref not in set_aside


def test_the_same_logs_still_contradict_an_unrelated_hypothesis():
    index = run4_refs()

    _, against, checks = check_citations("H9", "DNS resolution failures for an upstream vendor", [],
                                         ["observability/obs-1"], index)

    assert against == ["observability/obs-1"] and checks == []


def log_set(*records) -> EvidenceSet:
    return EvidenceSet(ref="l", tool="search_logs", source=EvidenceSource.LOGS, outcome=EvidenceOutcome.COLLECTED,
                       query={}, warnings=[], error=None,
                       items=[EvidenceItem(ref=f"l#{i}", record=r) for i, r in enumerate(records)])


def test_an_informational_log_sharing_the_words_still_contradicts():
    info = {"timestamp": "2026-09-01T10:03:00Z", "service": "payments-api", "level": "INFO",
            "message": "Connection pool size verified at 40; no pool waits"}
    index = RefIndex().add_sets([log_set(info)])

    _, against, _ = check_citations("H1", "Connection pool size reduced, pool waits", [], ["l#0"], index)

    assert against == ["l#0"]


def test_evidence_cited_both_ways_counts_as_neither():
    index = run4_refs()

    kept, against, checks = check_citations("H1", "Pool saturation", ["ev.logs#2"], ["ev.logs#2"], index)

    assert "ev.logs#2" not in kept and "ev.logs#2" not in against
    assert any(c.action == "removed_cited_both_ways" for c in checks)


# --- Metric direction ------------------------------------------------------------------------------------------------


def metric_index(values: list[float], name="error_rate_percent") -> RefIndex:
    records = [{"service": "svc", "metric_name": name, "timestamp": f"2026-09-01T09:{m:02d}:00Z", "value": v,
                "unit": "percent"} for m, v in zip(range(0, 5 * len(values), 5), values)]
    s = EvidenceSet(ref="m", tool="query_metrics", source=EvidenceSource.METRICS, outcome=EvidenceOutcome.COLLECTED,
                    query={}, warnings=[], error=None,
                    items=[EvidenceItem(ref=f"m#{i}", record=r) for i, r in enumerate(records)])
    return RefIndex().add_sets([s])


def test_a_sample_that_fell_contradicts_a_claimed_increase_of_that_metric():
    index = metric_index([5.0] * 9 + [0.5])  # error rate fell from 5% to 0.5%
    assert index.classify("m#9").evidence_class is EvidenceClass.DIRECT

    kept, against, checks = check_citations("H1", "Error rate increased after the deploy", ["m#9"], [], index)

    assert kept == [] and against == ["m#9"] and "fell" in checks[-1].detail


def test_direction_is_not_judged_when_the_claim_does_not_name_the_metric():
    index = metric_index([5.0] * 9 + [0.5])

    kept, _, _ = check_citations("H1", "Traffic increased after the deploy", ["m#9"], [], index)

    assert kept == ["m#9"]  # a semantic question: left to the model


def test_a_sample_that_rose_still_supports_a_claimed_increase():
    index = metric_index([0.2] * 9 + [7.6])

    kept, against, _ = check_citations("H1", "Error rate increased after the deploy", ["m#9"], [], index)

    assert kept == ["m#9"] and against == []


# --- Structured assessment ------------------------------------------------------------------------------------------


def test_assessment_of_run4_h1_is_structured_and_complete():
    index = run4_refs()
    _, claim, support, against = run4_h1()

    rows = assess_citations("H1", claim, support, against, index, affected_services=["payments-api"])

    assert [(r.ref, r.role) for r in rows] == [(s, "supporting") for s in support] + [(a, "contradicting") for a in against]
    pool = next(r for r in rows if r.ref == "observability/obs-2")
    assert pool.exists and pool.evidence_class is EvidenceClass.DIRECT and pool.sources == ["metrics"]
    assert pool.services == ["payments-api"] and pool.service_relevant and pool.flags == [] and not pool.unsupported
    assert next(r for r in rows if r.ref == "observability/obs-4").flags == ["contextual_only"]


def test_off_service_and_unknown_support_is_flagged_unsupported():
    index = run4_refs()
    from app.agents.evidence import EvidenceItem as Item
    orders = EvidenceSet(ref="c2", tool="get_change_history", source=EvidenceSource.CHANGES,
                         outcome=EvidenceOutcome.COLLECTED, query={}, warnings=[], error=None,
                         items=[Item(ref="c2#0", record={"timestamp": "2026-09-01T06:50:00Z", "change_id": "CHG-E01-1",
                                                         "service": "orders-service", "change_type": "deployment",
                                                         "status": "succeeded", "summary": "Routine dependency updates",
                                                         "details": {}})])
    index.add_sets([orders])

    rows = assess_citations("H5", "Payments slowed by pool waits", ["c2#0", "ev.nope#1"], [], index,
                            affected_services=["payments-api"])

    assert rows[0].flags == ["off_service"] and rows[0].service_relevant is False and rows[0].unsupported
    assert rows[1].flags == ["unknown_ref"] and not rows[1].exists and rows[1].unsupported


def test_missing_evidence_is_a_gap_not_proof_against_a_hypothesis(world):
    from tests.test_phase6_quality import decide, hyp

    _, refs = world
    rows = assess_citations("H1", "Pool size reduction", ["c#0", "l#0"], ["x"], refs)
    assert rows[-1].flags == ["missing_evidence"] and not refs.is_direct_evidence("x")

    a = decide(world, [hyp("H1", "medium", ["c#0", "l#0"], against=["x"])], critic_expected=False)
    assert next(c for c in a.checks if c.name == "no_material_contradiction").passed


from tests.test_phase6_quality import world  # noqa: E402,F401  (fixture)
