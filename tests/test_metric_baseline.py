"""Metric baselines come from samples before the incident's onset, never from the incident's own readings.

Regression for live run 2 of EVAL-01 (artifacts/live-runs/EVAL-01-run2): the model scoped the metrics window to
start at onset (10:00), so every sample was post-incident and 100% pool utilisation was classified "no_change"
("100 vs baseline 100") and shown as contradicting the correct cause. The pre-incident level is about 38%.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.agents import AgentConfig, OrchestratorAgent
from app.agents.contracts import InvestigationScope
from app.agents.evidence import (
    METRIC_BASELINE_LOOKBACK,
    REPORTED_AT_INPUT,
    EvidenceClass,
    EvidenceItem,
    EvidenceSet,
    RefIndex,
    evidence_sets,
    metric_query_start,
)
from app.baseline.single_agent import SingleAgentInvestigator, _State
from app.baseline.single_agent import ToolCall
from app.evaluation.dataset import load_cases
from app.evaluation.heuristic_model import HeuristicLLMClient
from app.orchestration.models import EvidenceOutcome
from app.schemas.evidence import EvidenceSource

EVAL_01 = {c.case_id: c for c in load_cases()}["EVAL-01"]
REPORTED = EVAL_01.incident.reported_at  # 10:12; the first ERROR log is at 10:02
END = "2026-09-01T10:45:00Z"


def evidence(ref: str, source: EvidenceSource, records: list[dict], reported_at: datetime | None = REPORTED,
             services: list[str] | None = None) -> EvidenceSet:
    context = {"incident_affected_services": services} if services is not None else {}
    return EvidenceSet(ref=ref, tool="t", source=source, outcome=EvidenceOutcome.COLLECTED, query={}, warnings=[],
                       error=None, items=[EvidenceItem(ref=f"{ref}#{i}", record=r) for i, r in enumerate(records)],
                       incident_reported_at=reported_at.isoformat() if reported_at else None, **context)


def eval01_index(metrics_start: str, *, with_logs: bool = True, reported_at: datetime | None = REPORTED) -> RefIndex:
    tools = EVAL_01.tools()
    metrics = tools["query_metrics"].run({"start": metrics_start, "end": END, "limit": 1000})
    sets = [evidence("ev.metrics", EvidenceSource.METRICS, [r.model_dump(mode="json") for r in metrics.records], reported_at)]
    if with_logs:
        logs = tools["search_logs"].run({"start": metrics_start, "end": END, "min_level": "WARNING"})
        sets.append(evidence("ev.logs", EvidenceSource.LOGS, [r.model_dump(mode="json") for r in logs.records], reported_at))
    return RefIndex().add_sets(sets)


def classify_sample(index: RefIndex, metric: str, at: str):
    for ref, source, r in index.records():
        if source is EvidenceSource.METRICS and r["metric_name"] == metric and r["timestamp"] == at:
            return index.classify(ref)
    raise AssertionError(f"no {metric} sample at {at}")


def series(name: str, values: list[tuple[str, float]]) -> list[dict]:
    return [{"service": "svc", "metric_name": name, "timestamp": t, "value": v, "unit": "u"} for t, v in values]


# --- EVAL-01 regression --------------------------------------------------------------------------------------------


def test_eval01_pool_at_100_percent_is_a_change_from_the_38_percent_pre_incident_baseline():
    index = eval01_index("2026-09-01T09:00:00Z")

    c = classify_sample(index, "db_pool_utilization_percent", "2026-09-01T10:15:00Z")

    assert c.evidence_class is EvidenceClass.DIRECT
    assert "= 100 vs baseline 38.4 (+161%): changed" in c.reason
    assert index.onset_estimate() == (datetime(2026, 9, 1, 10, 2, tzinfo=UTC), "the first ERROR log of the episode")


def test_a_window_starting_at_onset_never_calls_the_incidents_readings_normal():
    index = eval01_index("2026-09-01T10:00:00Z")  # live run 2's window

    for metric in ("db_pool_utilization_percent", "latency_p95_ms"):
        c = classify_sample(index, metric, "2026-09-01T10:15:00Z")
        assert c.evidence_class is EvidenceClass.CONTEXTUAL, c.reason  # previously NO_CHANGE ("100 vs baseline 100")
        assert "insufficient baseline (1 sample(s) before the estimated onset 2026-09-01T10:02:00Z" in c.reason


def test_stable_request_rate_is_still_classified_as_normal():
    c = classify_sample(eval01_index("2026-09-01T09:00:00Z"), "request_rate_rps", "2026-09-01T10:15:00Z")

    assert c.evidence_class is EvidenceClass.NO_CHANGE
    assert "= 123.42 vs baseline 120 (+3%): within the normal range" in c.reason


# --- Insufficient baseline -----------------------------------------------------------------------------------------


def test_no_samples_before_onset_is_an_insufficient_baseline_not_no_change():
    after = series("errors", [(f"2026-09-01T10:{m:02d}:00Z", 9.0) for m in (15, 20, 25, 30, 35)])
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, after)])

    c = index.classify("m#4")

    assert c.evidence_class is EvidenceClass.CONTEXTUAL
    assert "insufficient baseline (0 sample(s) before the estimated onset 2026-09-01T10:12:00Z, the reported time)" in c.reason


@pytest.mark.parametrize(("before", "expected"), [(2, EvidenceClass.CONTEXTUAL), (3, EvidenceClass.DIRECT)])
def test_at_least_three_pre_onset_samples_are_needed(before, expected):
    pre = [(f"2026-09-01T09:{m:02d}:00Z", 10.0) for m in range(0, 5 * before, 5)]
    post = [(f"2026-09-01T10:{m:02d}:00Z", 50.0) for m in (15, 20, 25)]
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, series("q", pre + post))])

    assert index.classify(f"m#{before + 2}").evidence_class is expected


def test_an_insufficient_baseline_cannot_be_turned_into_contradicting_evidence():
    from app.agents.grounding import check_citations

    index = eval01_index("2026-09-01T10:00:00Z")
    pool = next(ref for ref, src, r in index.records()
                if src is EvidenceSource.METRICS and r["metric_name"] == "db_pool_utilization_percent")

    support, contradicting, checks = check_citations("H1", "pool usage increased", [pool], [], index)

    assert support == [pool] and contradicting == [] and checks == []


def test_a_sample_retrieved_twice_counts_once_towards_the_baseline():
    pre = series("q", [("2026-09-01T09:00:00Z", 10.0), ("2026-09-01T09:05:00Z", 10.0)])
    post = series("q", [("2026-09-01T10:15:00Z", 50.0), ("2026-09-01T10:20:00Z", 50.0)])
    index = RefIndex().add_sets([evidence("a", EvidenceSource.METRICS, pre + post),
                                 evidence("b", EvidenceSource.METRICS, pre)])  # a follow-up query repeats them

    assert "2 sample(s) before" in index.classify("a#2").reason


# --- Onset estimate ------------------------------------------------------------------------------------------------


def log(t: str, level: str = "ERROR") -> dict:
    return {"timestamp": t, "service": "svc", "level": level, "message": "m"}


@pytest.mark.parametrize(
    ("logs", "expected", "basis"),
    [
        ([], "10:12", "the reported time"),
        ([log("2026-09-01T10:02:00Z"), log("2026-09-01T10:05:00Z")], "10:02", "the first ERROR log of the episode"),
        ([log("2026-09-01T08:30:00Z"), log("2026-09-01T10:02:00Z"), log("2026-09-01T10:05:00Z")], "10:02",
         "the first ERROR log of the episode"),  # a stray error 90 min earlier is not the onset
        ([log("2026-09-01T09:40:00Z", "WARNING"), log("2026-09-01T09:45:00Z", "WARNING")], "10:12", "the reported time"),
        ([log("2026-09-01T10:30:00Z"), log("2026-09-01T10:31:00Z")], "10:12", "the reported time"),  # after the report
    ],
)
def test_onset_estimate_uses_error_logs_bounded_by_the_reported_time(logs, expected, basis):
    index = RefIndex().add_sets([evidence("l", EvidenceSource.LOGS, logs)])

    onset, why = index.onset_estimate()

    assert onset.strftime("%H:%M") == expected and why == basis


def test_without_any_onset_information_the_earliest_third_is_used_as_before():
    values = [(f"2026-09-01T09:{m:02d}:00Z", 10.0) for m in range(0, 45, 5)] + [("2026-09-01T09:45:00Z", 50.0)]
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, series("q", values), reported_at=None)])

    assert index.onset_estimate() == (None, "unknown")
    assert index.classify("m#9").evidence_class is EvidenceClass.DIRECT  # 3 of 10 samples form the baseline


def test_the_reported_time_is_code_only_and_never_shown_to_the_model():
    s = evidence("m", EvidenceSource.METRICS, [])

    assert s.incident_reported_at and "incident_reported_at" not in s.model_dump()


# --- What the model is shown -----------------------------------------------------------------------------------------


def pool_summary(metrics_start: str):
    from app.agents.observability import summarise_metrics

    metrics = EVAL_01.tools()["query_metrics"].run({"start": metrics_start, "end": END, "limit": 1000})
    sets = [evidence("ev.metrics", EvidenceSource.METRICS, [r.model_dump(mode="json") for r in metrics.records])]
    index = eval01_index(metrics_start)  # the same metrics plus the logs that date the onset
    return next(m for m in summarise_metrics(sets, index) if m.metric_name == "db_pool_utilization_percent")


def test_metric_summary_shows_the_model_the_pre_incident_baseline():
    s = pool_summary("2026-09-01T09:00:00Z")

    assert s.baseline == 38.38 and s.peak == 100
    assert s.baseline_basis.startswith("median of 13 samples before the estimated onset 2026-09-01T10:02:00Z")


def test_metric_summary_never_shows_an_incident_reading_as_the_baseline():
    s = pool_summary("2026-09-01T10:00:00Z")  # live run 2 showed the model "baseline 100" here

    assert s.baseline is None
    assert s.baseline_basis.startswith("insufficient baseline (1 sample(s) before the estimated onset")


# --- Fetching a baseline ---------------------------------------------------------------------------------------------


def scope(start: datetime) -> InvestigationScope:
    return InvestigationScope(window_start=start, window_end=REPORTED + timedelta(minutes=33),
                              change_lookback_start=start - timedelta(hours=24), focus_services=["payments-api"],
                              keywords=[], rationale="r")


def test_planner_fetches_metrics_from_before_a_window_that_starts_at_onset():
    onset_window = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    plan = {t.task_id: t for t in OrchestratorAgent(llm=None, tools=EVAL_01.tools()).plan_tasks(scope(onset_window), REPORTED)}

    assert plan["ev.metrics"].inputs["query"]["start"] == (REPORTED - METRIC_BASELINE_LOOKBACK).isoformat()  # 09:12
    assert plan["ev.logs"].inputs["query"]["start"] == onset_window.isoformat()  # other tools keep the model's window
    assert all(t.inputs[REPORTED_AT_INPUT] == REPORTED.isoformat() for i, t in plan.items() if i.startswith("ev."))


def test_planner_keeps_a_window_that_already_starts_early_enough():
    early = REPORTED - timedelta(minutes=90)

    assert metric_query_start(early, REPORTED) == early
    assert metric_query_start(early, None) == early


def test_single_agent_baseline_gets_the_same_pre_incident_metrics():
    investigator = SingleAgentInvestigator(llm=None, tools=EVAL_01.tools())
    state = _State(REPORTED)
    call = ToolCall(tool="query_metrics", query_json='{"start": "2026-09-01T10:00:00Z", "end": "' + END + '"}', reason="r")

    accepted, rejected = investigator._validate_calls([call], state, 5)
    tool, query = accepted[0]
    result = asyncio.run(investigator._run_tool("b1.1", tool, query, state))

    assert rejected == [] and query["start"] == (REPORTED - METRIC_BASELINE_LOOKBACK).isoformat()
    assert result.incident_reported_at == REPORTED.isoformat()
    pool = classify_sample(RefIndex().add_sets([result]), "db_pool_utilization_percent", "2026-09-01T10:15:00Z")
    assert pool.evidence_class is EvidenceClass.DIRECT


class OnsetWindowModel(HeuristicLLMClient):
    """The offline stand-in, except that the orchestrator scopes the window to start at onset, as in live run 2."""

    async def generate(self, request, output_type):
        out = await super().generate(request, output_type)
        if request.agent == "orchestrator":
            out = out.model_copy(update={"window_start": datetime(2026, 9, 1, 10, 0, tzinfo=UTC)})
        return out


def test_end_to_end_a_run2_style_window_still_shows_the_pool_saturation_as_a_change():
    orchestrator = OrchestratorAgent(OnsetWindowModel(), EVAL_01.tools(), AgentConfig())
    result = asyncio.run(orchestrator.investigate(EVAL_01.incident, investigation_id="inv-baseline"))
    tasks = result.investigation.tasks
    index = RefIndex().add_sets(evidence_sets(tasks))

    assert tasks["ev.logs"].spec.inputs["query"]["start"].startswith("2026-09-01T10:00:00")
    assert tasks["ev.metrics"].spec.inputs["query"]["start"].startswith("2026-09-01T09:12:00")
    pool = classify_sample(index, "db_pool_utilization_percent", "2026-09-01T10:15:00Z")
    assert pool.evidence_class is EvidenceClass.DIRECT and "vs baseline 38" in pool.reason
    for h in result.report.report.ranked_hypotheses:
        for ref in h.contradicting_refs:
            assert "db_pool_utilization_percent" not in index.classify(ref).reason


# --- Review fixes ----------------------------------------------------------------------------------------------------


def eval01_records(tool: str, start: str, **query) -> list[dict]:
    return [r.model_dump(mode="json") for r in EVAL_01.tools()[tool].run({"start": start, "end": END, **query}).records]


def other_errors(*times: str, service: str = "orders-service") -> list[dict]:
    return [{"timestamp": t, "service": service, "level": "ERROR", "message": "unrelated failure"} for t in times]


def test_errors_in_an_unrelated_service_do_not_move_the_onset():
    metrics = eval01_records("query_metrics", "2026-09-01T09:12:00Z", limit=1000)
    logs = eval01_records("search_logs", "2026-09-01T10:00:00Z", min_level="WARNING")
    logs += other_errors("2026-09-01T09:20:00Z", "2026-09-01T09:25:00Z")  # an earlier episode elsewhere
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, metrics, services=["payments-api"]),
                                 evidence("l", EvidenceSource.LOGS, logs, services=["payments-api"])])

    assert index.onset_estimate() == (datetime(2026, 9, 1, 10, 2, tzinfo=UTC), "the first ERROR log of the episode")
    pool = classify_sample(index, "db_pool_utilization_percent", "2026-09-01T10:15:00Z")
    assert pool.evidence_class is EvidenceClass.DIRECT and "vs baseline 38" in pool.reason


def test_without_known_affected_services_every_services_errors_count():
    logs = other_errors("2026-09-01T09:20:00Z", "2026-09-01T09:25:00Z")
    index = RefIndex().add_sets([evidence("l", EvidenceSource.LOGS, logs, services=[])])

    assert index.onset_estimate()[0] == datetime(2026, 9, 1, 9, 20, tzinfo=UTC)


def test_a_single_stray_error_does_not_downgrade_a_usable_baseline():
    metrics = eval01_records("query_metrics", "2026-09-01T09:12:00Z", limit=1000)
    stray = other_errors("2026-09-01T09:22:00Z", service="payments-api")  # 40 min before the incident, alone
    index = RefIndex().add_sets([evidence("m", EvidenceSource.METRICS, metrics, services=["payments-api"]),
                                 evidence("l", EvidenceSource.LOGS, stray, services=["payments-api"])])

    assert index.onset_estimate() == (REPORTED, "the reported time")  # a lone error is not an episode
    pool = classify_sample(index, "db_pool_utilization_percent", "2026-09-01T10:15:00Z")
    assert pool.evidence_class is EvidenceClass.DIRECT


def test_planner_and_single_agent_carry_the_affected_services():
    onset_window = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    plan = OrchestratorAgent(llm=None, tools=EVAL_01.tools()).plan_tasks(scope(onset_window), REPORTED, ["payments-api"])
    assert all(t.inputs["incident_affected_services"] == ["payments-api"] for t in plan if t.task_id.startswith("ev."))

    investigator = SingleAgentInvestigator(llm=None, tools=EVAL_01.tools())
    state = _State(REPORTED, ["payments-api"])
    call = ToolCall(tool="query_metrics", query_json='{"start": "2026-09-01T10:00:00Z", "end": "' + END + '"}', reason="r")
    tool, query = investigator._validate_calls([call], state, 5)[0][0]
    result = asyncio.run(investigator._run_tool("b1.1", tool, query, state))
    assert result.incident_affected_services == ["payments-api"]


def unknown_onset_series(n: int) -> list[dict]:
    """n samples at 5-minute steps: flat at 10, then the last one jumps to 50. No reported time, no error logs."""
    values = [10.0] * (n - 1) + [50.0]
    return [{"service": "svc", "metric_name": "q", "timestamp": f"2026-09-01T{9 + m // 60:02d}:{m % 60:02d}:00Z",
             "value": v, "unit": "u"} for m, v in zip(range(0, 5 * n, 5), values)]


@pytest.mark.parametrize(("n", "eligible"), [(2, 0), (3, 1), (6, 2), (9, 3)])
def test_unknown_onset_needs_three_eligible_samples_in_classifier_and_summary(n, eligible):
    from app.agents.observability import summarise_metrics

    sets = [evidence("m", EvidenceSource.METRICS, unknown_onset_series(n), reported_at=None)]
    index = RefIndex().add_sets(sets)
    c, summary = index.classify(f"m#{n - 1}"), summarise_metrics(sets, index)[0]

    assert index.onset_estimate() == (None, "unknown")
    if eligible < 3:
        assert c.evidence_class is EvidenceClass.CONTEXTUAL and "insufficient baseline" in c.reason
        assert summary.baseline is None and summary.baseline_basis.startswith(f"insufficient baseline ({eligible} sample(s)")
    else:
        assert c.evidence_class is EvidenceClass.DIRECT and summary.baseline == 10.0


def test_a_series_loaded_twice_gives_the_same_summary_and_classification():
    from app.agents.observability import summarise_metrics

    records = eval01_records("query_metrics", "2026-09-01T09:00:00Z", limit=1000)
    once = [evidence("a", EvidenceSource.METRICS, records)]
    twice = once + [evidence("b", EvidenceSource.METRICS, records)]
    one, two = RefIndex().add_sets(once), RefIndex().add_sets(twice)

    def pool(sets, index):
        return next(m for m in summarise_metrics(sets, index) if m.metric_name == "db_pool_utilization_percent")

    s1, s2 = pool(once, one), pool(twice, two)
    assert (s2.samples, s2.minimum, s2.peak, s2.last, s2.baseline) == (s1.samples, s1.minimum, s1.peak, s1.last, s1.baseline)
    assert s2.samples == 22
    pool_ref = next(ref for ref, src, r in one.records() if r.get("metric_name") == "db_pool_utilization_percent"
                    and r["timestamp"] == "2026-09-01T10:15:00Z")
    assert one.classify(pool_ref).reason == two.classify(pool_ref).reason
