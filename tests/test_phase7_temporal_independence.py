"""Phase 7: timeline consistency and evidence independence (offline; synthetic records and scripted/heuristic models)."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.agents import AgentConfig
from app.agents.adjudication import adjudicate
from app.agents.contracts import Hypothesis, ReportOutput
from app.agents.evidence import EvidenceClass, EvidenceItem, EvidenceSet, RefIndex
from app.agents.independence import assess_independence, group_observations
from app.agents.inventory import build_inventory
from app.agents.report_rules import finalize_report
from app.agents.temporal import assess, parse_instant
from app.baseline import BaselineConfig
from app.evaluation.dataset import load_cases
from app.evaluation.heuristic_model import HeuristicLLMClient
from app.evaluation.runner import run_case
from app.orchestration.models import EvidenceOutcome
from app.schemas.evidence import EvidenceSource

DAY = "2026-09-01T"


def t(hhmm: str) -> str:
    return f"{DAY}{hhmm}:00Z"


def log(hhmm, msg="Request failed", level="ERROR", service="api", trace=None):
    r = {"timestamp": t(hhmm), "service": service, "level": level, "message": msg}
    if trace:
        r["trace_id"] = trace
    return r


def change(cid, when, ctype="deployment", status="succeeded", service="api", version=None, details=None):
    r = {"timestamp": when, "change_id": cid, "service": service, "change_type": ctype, "status": status,
         "summary": cid, "details": details or {}}
    if version:
        r["version"] = version
    return r


def metric(hhmm, value, name="latency_ms", service="api"):
    return {"timestamp": t(hhmm), "service": service, "metric_name": name, "value": value, "unit": "ms"}


def world(logs=(), changes=(), metrics=(), dup_logs=()):
    def s(ref, source, tool, recs):
        return EvidenceSet(ref=ref, tool=tool, source=source, outcome=EvidenceOutcome.COLLECTED, query={}, warnings=[],
                           error=None, items=[EvidenceItem(ref=f"{ref}#{i}", record=r) for i, r in enumerate(recs)])
    sets = [s("l", EvidenceSource.LOGS, "search_logs", list(logs)),
            s("c", EvidenceSource.CHANGES, "get_change_history", list(changes)),
            s("m", EvidenceSource.METRICS, "query_metrics", list(metrics)),
            s("l2", EvidenceSource.LOGS, "search_logs", list(dup_logs))]
    return sets, RefIndex().add_sets(sets)


def hyp(support, hid="H1", confidence="medium", against=()):
    return Hypothesis(hypothesis_id=hid, title=hid, description="d", supporting_refs=list(support),
                      contradicting_refs=list(against), missing_information=[], confidence=confidence,
                      confidence_rationale="r")


# --- Timestamp parsing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01T09:30:00Z", datetime(2026, 9, 1, 9, 30, tzinfo=UTC)),
        ("2026-09-01T15:00:00+05:30", datetime(2026, 9, 1, 9, 30, tzinfo=UTC)),  # same instant, other zone
        ("2026-09-01T09:30:00.250+00:00", datetime(2026, 9, 1, 9, 30, 0, 250000, tzinfo=UTC)),
        (datetime(2026, 9, 1, 4, 30, tzinfo=timezone(timedelta(hours=-5))), datetime(2026, 9, 1, 9, 30, tzinfo=UTC)),
        ("2026-09-01T09:30:00", None),  # no timezone: ambiguous instant
        (datetime(2026, 9, 1, 9, 30), None),
        ("not-a-time", None),
        ("", None),
        (None, None),
        (1693560600, None),  # epoch numbers are not guessed
    ],
)
def test_parse_instant(value, expected):
    assert parse_instant(value) == expected


# --- Timeline scenarios ---------------------------------------------------------------------------


def test_1_symptoms_begin_before_the_deployment():
    sets, refs = world(logs=[log("09:30"), log("09:50")], changes=[change("D1", t("09:45"))])
    a = assess(hyp(["c#0", "l#1"]), refs, ["api"])

    assert a.status == "change_follows_onset" and a.blocks_cause
    assert a.onset == t("09:30") and a.onset_refs == ["l#0"]
    assert a.changes[0].relation == "follows_onset" and "cannot explain the onset" in a.changes[0].detail


def test_2_symptoms_begin_after_the_deployment():
    sets, refs = world(logs=[log("09:30")], changes=[change("D1", t("09:10"))])
    a = assess(hyp(["c#0", "l#0"]), refs, ["api"])

    assert a.status == "consistent" and not a.blocks_cause and a.changes[0].relation == "precedes_onset"


def test_3_a_later_deployment_may_have_worsened_an_existing_incident():
    series = [metric(f"09:{m:02d}", v) for m, v in [(0, 100), (5, 102), (10, 99), (15, 101), (20, 300), (25, 310),
                                                     (30, 305), (35, 600), (40, 620)]]
    sets, refs = world(changes=[change("D1", t("09:32"))], metrics=series)
    a = assess(hyp(["c#0", "m#4"]), refs, ["api"])

    assert a.blocks_cause and a.may_have_worsened and a.worsening_refs == ["m#8"]
    leader = hyp(["c#0", "m#4"])
    out, _ = finalize_report(_empty_report(), hypotheses=[leader], refs=refs, inventory=build_inventory(sets),
                             adjudication=_adjudicate(sets, refs, [leader], [a]), known_limitations=[], timelines=[a])
    text = out.interpretations[0].text
    assert "cannot explain how the incident began" in text and "may have worsened the existing incident" in text
    assert out.interpretations[0].evidence_refs == ["c#0", "m#4", "m#8"]

    flat_after = [metric(f"09:{m:02d}", v) for m, v in [(0, 100), (5, 102), (10, 99), (15, 101), (20, 300), (25, 310),
                                                         (30, 305), (35, 302), (40, 298)]]
    _, refs2 = world(changes=[change("D1", t("09:32"))], metrics=flat_after)
    assert not assess(hyp(["c#0", "m#4"]), refs2, ["api"]).may_have_worsened  # later, but no evidence it worsened


def test_4_missing_timestamp_does_not_discard_the_hypothesis():
    sets, refs = world(logs=[log("09:30")], changes=[change("D1", "")])
    a = assess(hyp(["c#0", "l#0"]), refs, ["api"])

    assert a.status == "unknown" and not a.blocks_cause
    assert a.changes[0].relation == "unknown" and a.changes[0].time is None and "timestamp missing" in a.changes[0].detail


def test_5_time_zones_are_compared_as_instants_not_strings():
    # 15:00+05:30 is 09:30Z, the same instant as the symptom: ordering cannot be determined.
    _, same = world(logs=[log("09:30")], changes=[change("D1", f"{DAY}15:00:00+05:30")])
    # 14:45+05:30 is 09:15Z: before the symptom, although "14:45" > "09:30" as text.
    _, before = world(logs=[log("09:30")], changes=[change("D1", f"{DAY}14:45:00+05:30")])

    a, b = assess(hyp(["c#0", "l#0"]), same, ["api"]), assess(hyp(["c#0", "l#0"]), before, ["api"])
    assert a.changes[0].relation == "ambiguous" and a.changes[0].time == t("09:30")
    assert b.changes[0].relation == "precedes_onset" and b.changes[0].time == t("09:15")


@pytest.mark.parametrize("when", ["not-a-time", f"{DAY}09:20:00", "2026-13-45T99:00:00Z"])
def test_6a_invalid_or_zoneless_timestamps_are_unknown(when):
    _, refs = world(logs=[log("09:30")], changes=[change("D1", when)])
    a = assess(hyp(["c#0", "l#0"]), refs, ["api"])

    assert a.changes[0].relation == "unknown" and not a.blocks_cause


def test_6b_orderings_within_precision_are_ambiguous():
    _, close = world(logs=[log("09:30")], changes=[change("D1", f"{DAY}09:30:40Z")])
    series = [metric(f"09:{m:02d}", v) for m, v in [(0, 100), (5, 101), (10, 99), (15, 100), (20, 100), (25, 100), (30, 400)]]
    _, sampled = world(changes=[change("D1", t("09:27"))], metrics=series)  # metric shows the change by 09:30, sampled every 5 min

    assert assess(hyp(["c#0", "l#0"]), close, ["api"]).status == "ordering_ambiguous"
    s = assess(hyp(["c#0", "m#6"]), sampled, ["api"])
    assert s.status == "ordering_ambiguous" and s.onset_precision_seconds == 300


def rollback_of_d1(when="09:20", status="succeeded"):
    """A rollback record that demonstrably reverts CHG-701 (version 2.0.0 -> 1.9.0)."""
    return change("RB1", t(when), ctype="rollback", status=status, version="1.9.0") | {"previous_version": "2.0.0"}


D1 = change("CHG-701", t("09:00"), version="2.0.0")  # ids under 3 characters are never matched


def test_7_a_change_before_symptoms_can_still_be_ruled_out_by_its_rollback():
    persisting = [log("09:10"), log("09:15"), log("09:40")]
    _, refs = world(logs=persisting, changes=[D1, rollback_of_d1()])
    a = assess(hyp(["c#0", "l#0"]), refs, ["api"])

    assert a.status == "persisted_after_rollback" and a.blocks_cause
    assert a.changes[0].rollback_ref == "c#1" and a.changes[0].persisted_refs == ["l#2"]

    _, recovered = world(logs=[log("09:10"), log("09:15")], changes=[D1, rollback_of_d1()])
    assert assess(hyp(["c#0", "l#0"]), recovered, ["api"]).status == "consistent"  # symptoms stopped after rollback

    _, failed_rb = world(logs=persisting, changes=[D1, rollback_of_d1(status="failed")])
    assert assess(hyp(["c#0", "l#0"]), failed_rb, ["api"]).status == "consistent"  # a failed rollback proves nothing


@pytest.mark.parametrize(
    ("rollback", "excluded"),
    [
        # Phase 9 regression: a rollback of a DIFFERENT change on the same service used to exclude D1.
        (change("RB9", t("09:20"), ctype="rollback", version="1.4.0") | {"previous_version": "1.5.0",
                                                                           "summary": "Roll back the search widget"}, False),
        (change("RB2", t("09:20"), ctype="rollback"), False),  # no linking information: never an exclusion
        (change("RB3", t("09:20"), ctype="rollback") | {"summary": "Revert CHG-701 (bad release)"}, True),  # names the change id
        (change("RB4", t("09:20"), ctype="rollback") | {"summary": "Roll back to before 2.0.0"}, True),  # names the version
        (rollback_of_d1(), True),  # previous_version is D1's version
    ],
)
def test_only_a_rollback_that_demonstrably_reverts_the_change_can_exclude_it(rollback, excluded):
    _, refs = world(logs=[log("09:10"), log("09:15"), log("09:40")], changes=[D1, rollback])
    a = assess(hyp(["c#0", "l#0"]), refs, ["api"])

    assert a.blocks_cause is excluded
    assert a.status == ("persisted_after_rollback" if excluded else "consistent")


def test_failed_change_is_flagged_as_uncertain_and_non_change_hypotheses_are_not_applicable():
    _, refs = world(logs=[log("09:30")], changes=[change("D1", t("09:10"), status="failed")])

    assert "did not complete" in assess(hyp(["c#0", "l#0"]), refs, ["api"]).changes[0].detail
    assert assess(hyp(["l#0"]), refs, ["api"]).status == "not_applicable"


def test_unrelated_warnings_and_other_services_do_not_set_onset():
    sets, refs = world(logs=[log("09:05", level="WARNING"), log("09:06", service="other"), log("09:30")],
                       changes=[change("D1", t("09:10"))])
    a = assess(hyp(["c#0", "l#2"]), refs, ["api"])

    assert a.onset == t("09:30") and a.status == "consistent"


def test_adjudication_blocks_a_leader_whose_change_follows_onset():
    sets, refs = world(logs=[log("09:30"), log("09:50")], metrics=[metric(f"09:{m:02d}", v) for m, v in
                       [(0, 100), (5, 101), (10, 99), (15, 100), (20, 100), (25, 100), (35, 900)]],
                       changes=[change("D1", t("09:45"))])
    leader = hyp(["c#0", "l#1", "m#6"])
    a = _adjudicate(sets, refs, [leader], [assess(leader, refs, ["api"])])

    assert a.decision == "inconclusive"
    assert any(b.startswith("temporal_consistency: change_follows_onset") for b in a.blocking_issues)


# --- Evidence independence ---------------------------------------------------------------------------


def test_duplicate_records_are_one_observation():
    _, refs = world(logs=[log("09:30", "Disk full")], dup_logs=[log("09:30", "Disk full")])
    a = assess_independence(["l#0", "l2#0"], refs)

    assert len(a.groups) == 1 and a.status == "none"
    assert a.groups[0].refs == ["l#0", "l2#0"] and "duplicate record" in a.groups[0].merged_because[0]


def test_log_echo_of_a_change_is_the_same_event():
    _, refs = world(logs=[log("09:31", "Config reloaded: db.pool.max_size=8", level="INFO")],
                    changes=[change("C1", t("09:30"), ctype="config_change", details={"key": "db.pool.max_size"})])
    _, versioned = world(logs=[log("09:31", "Starting api v6.0.0", level="INFO")], changes=[change("D1", t("09:30"), version="6.0.0")])
    _, unrelated = world(logs=[log("09:31", "Starting api v6.0.10", level="INFO")], changes=[change("D1", t("09:30"), version="6.0.1")])

    echo = assess_independence(["c#0", "l#0"], refs)
    assert echo.status == "none" and echo.groups[0].primary_source == "changes"
    assert "echoes the change" in echo.detail
    assert assess_independence(["c#0", "l#0"], versioned).status == "none"
    assert assess_independence(["c#0", "l#0"], unrelated).status == "independent"  # "6.0.1" is not "6.0.10"


def test_same_request_seen_twice_is_one_observation():
    _, refs = world(logs=[log("09:30", "Timeout", trace="t-1"), log("09:30", "Upstream 504", service="gw", trace="t-1"),
                          log("09:31", "Timeout", trace="t-2")])
    groups = group_observations(["l#0", "l#1", "l#2"], refs)

    assert [g.refs for g in groups] == [["l#0", "l#1"], ["l#2"]]


def test_independent_sources_and_unknown_independence():
    series = [metric(f"09:{m:02d}", v) for m, v in [(0, 100), (5, 101), (10, 99), (15, 100), (20, 100), (25, 100), (30, 500)]]
    _, refs = world(logs=[log("09:30", "Timeout"), log("09:31", "Pool exhausted", service="db")],
                    changes=[change("D1", t("09:20"))], metrics=series)

    independent = assess_independence(["c#0", "m#6"], refs)
    unknown = assess_independence(["l#0", "l#1"], refs)  # two different log lines: maybe the same failure
    assert independent.status == "independent" and independent.independent_sources == ["changes", "metrics"]
    assert unknown.status == "unknown" and unknown.independent_sources == []
    assert "independence cannot be established" in unknown.detail


def test_adjudication_does_not_count_an_echo_as_corroboration():
    sets, refs = world(logs=[log("09:31", "Config reloaded: db.pool.max_size=8", level="INFO")],
                       changes=[change("C1", t("09:30"), ctype="config_change", details={"key": "db.pool.max_size"})])
    a = _adjudicate(sets, refs, [hyp(["c#0", "l#0"])], None)

    assert a.decision == "inconclusive"
    assert any(b.startswith("direct_support: independence none") and "echoes the change" in b for b in a.blocking_issues)


# --- Metric noise (regression found while adding timeline checks) -----------------------------------------


def test_metric_noise_on_a_small_baseline_is_not_a_change():
    jitter = [metric(f"09:{m:02d}", v, "error_rate") for m, v in
              [(0, 0.2), (5, 0.2), (10, 0.3), (15, 0.2), (20, 0.2), (25, 0.3), (30, 0.3), (35, 0.2), (40, 1.8)]]
    _, refs = world(metrics=jitter)

    assert refs.classify("m#6").evidence_class is EvidenceClass.NO_CHANGE  # 0.3 within the 0.2/0.3 alternation
    assert refs.classify("m#8").evidence_class is EvidenceClass.DIRECT


def test_a_change_point_inside_the_baseline_window_is_still_detected():
    pool = [metric(f"09:{m:02d}", v, "pool_pct") for m, v in
            [(0, 36), (5, 38), (10, 37), (15, 40), (20, 39), (25, 95), (30, 100), (35, 100), (40, 100),
             (45, 100), (50, 100), (55, 100), (59, 100), (58, 100), (57, 100), (56, 100), (54, 100), (53, 100)]]
    _, refs = world(metrics=pool)

    assert refs.classify("m#5").evidence_class is EvidenceClass.DIRECT  # median baseline is robust to it


# --- End to end on the evaluation worlds (offline heuristic, both approaches) ---------------------------------


CASES = {c.case_id: c for c in load_cases()}


@pytest.mark.parametrize("approach", ["multi_agent", "single_agent"])
@pytest.mark.parametrize(("case_id", "status"), [("EVAL-02", "change_follows_onset"), ("EVAL-10", "persisted_after_rollback")])
def test_misleading_deploys_are_no_longer_named_as_the_cause(approach, case_id, status):
    record, _ = asyncio.run(run_case(CASES[case_id], approach, HeuristicLLMClient, "test", AgentConfig(), BaselineConfig()))

    assert record.conclusion == "inconclusive"
    top = record.hypotheses[0]
    assert "deploy" in top.title.lower() or "orm" in top.title.lower() or "template" in top.title.lower()
    assert top.confidence == "low"


def test_timeline_reasons_reach_the_report_for_eval_02():
    from app.agents import OrchestratorAgent

    result = asyncio.run(OrchestratorAgent(HeuristicLLMClient(), CASES["EVAL-02"].tools(), AgentConfig())
                         .investigate(CASES["EVAL-02"].incident))
    h1 = next(t for t in result.report.temporal_assessments if t.hypothesis_id == "H1")

    # The orders-service error-rate metric shows the change at 13:35, before the first ERROR log at 13:37.
    assert h1.status == "change_follows_onset" and h1.onset == "2026-09-02T13:35:00Z"
    assert any("CHG-E02-1" in i.text and "cannot explain how the incident began" in i.text
               for i in result.report.report.interpretations)
    assert any(c.name == "temporal_consistency" for c in result.report.adjudication.checks)


# --- helpers -------------------------------------------------------------------------------------------------


def _empty_report():
    return ReportOutput(conclusion="inconclusive", headline="h", summary="s", ranked_hypotheses=[], facts=[],
                        interpretations=[], assumptions=["a"], would_confirm=[], would_disconfirm=[], limitations=[],
                        corrective_actions=[], open_questions=[])


def _adjudicate(sets, refs, hypotheses, timelines):
    return adjudicate(hypotheses, False, refs, build_inventory(sets), final_critique=None, critiques=None,
                      critic_expected=False, timelines=timelines)


def test_timeline_checks_are_shown_in_the_report_view_and_markdown():
    from app.agents import OrchestratorAgent
    from app.product.report_view import build_view, render_markdown

    result = asyncio.run(OrchestratorAgent(HeuristicLLMClient(), CASES["EVAL-10"].tools(), AgentConfig())
                         .investigate(CASES["EVAL-10"].incident))
    view = build_view(result, evidence_source="EVAL-10", model="offline-heuristic")
    md = render_markdown(view)

    assert any(t.status == "persisted_after_rollback" for t in view.timeline_checks)
    assert "## Timeline checks" in md and "persisted after rollback" in md and "CHG-E10-2" in md
    assert view.unresolved_citations == []


# --- Phase 9: onset is the first symptom of an episode, not a stray earlier error ---------------------


def test_a_stray_earlier_error_does_not_exclude_the_true_cause():
    _, refs = world(logs=[log("08:50", "Transient DNS hiccup"), log("10:02"), log("10:05"), log("10:14")],
                    changes=[change("CHG-E01-2", t("09:55"))])
    a = assess(hyp(["c#0", "l#1"]), refs, ["api"])

    assert a.status == "consistent" and not a.blocks_cause and a.onset == t("10:02")
    assert "earlier isolated symptom(s) not counted as onset: l#0" in a.detail


def test_an_early_symptom_episode_still_excludes_a_later_change():
    _, refs = world(logs=[log("09:20"), log("09:28"), log("10:02")], changes=[change("CHG-1", t("09:55"))])
    a = assess(hyp(["c#0", "l#2"]), refs, ["api"])

    assert a.status == "change_follows_onset" and a.blocks_cause and a.onset == t("09:20")


def test_all_isolated_symptoms_establish_no_onset_and_never_exclude():
    _, refs = world(logs=[log("08:00"), log("09:00"), log("10:00")], changes=[change("CHG-1", t("09:30"))])
    a = assess(hyp(["c#0", "l#2"]), refs, ["api"])

    assert a.status == "unknown" and not a.blocks_cause and a.onset is None
    assert "every symptom is isolated" in a.detail


def test_a_single_symptom_is_still_used_as_onset():
    _, refs = world(logs=[log("09:30")], changes=[change("CHG-1", t("09:45"))])

    assert assess(hyp(["c#0", "l#0"]), refs, ["api"]).status == "change_follows_onset"
