"""Metric calculations on hand-built runs, including inconclusive, wrong and failed outcomes."""

import json

import pytest

from app.evaluation.dataset import load_labels
from app.evaluation.metrics import aggregate, grounding, matches, score
from app.evaluation.normalize import NormalizedHypothesis, RefInfo, RunRecord

LABELS = load_labels()


def hyp(title, refs=(), contra=(), confidence="medium", rank=1, assessment=""):
    return NormalizedHypothesis(rank=rank, hypothesis_id=f"H{rank}", title=title, assessment=assessment,
                                confidence=confidence, supporting_refs=list(refs), contradicting_refs=list(contra))


def record(data, outcome="collected"):
    return RefInfo(kind="record", outcome=outcome, text=json.dumps(data, sort_keys=True), resolves_to=[])


def run(case_id="EVAL-01", conclusion="probable_cause_identified", hyps=(), evidence=None, status="completed",
        facts=(), tokens=(None, None), approach="multi_agent"):
    return RunRecord(case_id=case_id, approach=approach, mode="test", status=status, error=None,
                     conclusion=None if status == "failed" else conclusion, report_generated_by="model",
                     hypotheses=list(hyps), fact_refs=list(facts), evidence=evidence or {}, tool_calls=3,
                     model_calls=2, invalid_outputs=0, latency_seconds=1.0, input_tokens=tokens[0], output_tokens=tokens[1])


def test_matches_requires_every_group():
    assert matches("DB pool size reduced", [["pool"], ["size", "reduc"]])
    assert not matches("DB pool exhausted", [["pool"], ["size", "reduc"]])
    assert matches("POOL Size", [["pool"], ["size"]])  # case-insensitive


@pytest.mark.parametrize(
    ("case_id", "conclusion", "title", "expected"),
    [
        ("EVAL-01", "probable_cause_identified", "Pool size reduced by config", "correct"),
        ("EVAL-02", "probable_cause_identified", "orders deploy v6.0.0 template", "wrong_conclusion"),
        ("EVAL-04", "inconclusive", "fraud-check timeouts", "acceptable_inconclusive"),
        ("EVAL-01", "inconclusive", "Pool size reduced", "missed_inconclusive"),
        ("EVAL-09", "inconclusive", "anything", "correct_inconclusive"),
        ("EVAL-09", "probable_cause_identified", "frontend slow", "wrong_conclusion"),
    ],
)
def test_outcome_classification(case_id, conclusion, title, expected):
    s = score(run(case_id, conclusion, [hyp(title)]), LABELS[case_id])

    assert s.outcome == expected
    assert s.strict_correct == (expected in ("correct", "correct_inconclusive"))
    assert s.lenient_correct == (expected in ("correct", "correct_inconclusive", "acceptable_inconclusive"))


def test_failed_run_is_scored_not_dropped():
    s = score(run(status="failed"), LABELS["EVAL-01"])

    assert s.outcome == "failed" and not s.lenient_correct and s.top1_match is False


def test_acceptable_alternative_counts_and_is_named():
    s = score(run("EVAL-08", hyps=[hyp("fraud-check timeouts amplified by retry refactor")]), LABELS["EVAL-08"])

    assert s.outcome == "correct" and s.matched_label == "fraud-check-retry-change"


def test_distractor_top1_and_top3():
    hyps = [hyp("orders v6.0.0 deploy", rank=1), hyp("db disk full", rank=2, confidence="low")]
    s = score(run("EVAL-02", hyps=hyps), LABELS["EVAL-02"])

    assert s.distractor_top1 and s.top1_match is False and s.top3_match is True


def test_must_consider_coverage():
    s = score(run("EVAL-08", hyps=[hyp("pool exhausted"), hyp("fraud-check slow", rank=2)]), LABELS["EVAL-08"])
    half = score(run("EVAL-08", hyps=[hyp("pool exhausted")]), LABELS["EVAL-08"])

    assert s.must_consider_coverage == 1.0 and half.must_consider_coverage == 0.5


def test_grounding_counts_existing_available_and_lexical_support():
    evidence = {
        "ev.c#0": record({"change_id": "CHG-E01-2", "summary": "Reduce database connection pool size"}),
        "ev.l#0": record({"message": "unrelated heartbeat"}),
        "ev.m": RefInfo(kind="result", outcome="failed", text="query_metrics", resolves_to=[]),
        "dependency/dep-1": RefInfo(kind="finding", outcome=None, text="pool change", resolves_to=["ev.c#0"]),
    }
    h = hyp("Connection pool reduced", refs=["ev.c#0", "ev.l#0", "dependency/dep-1", "ev.m", "ev.x#9"])
    g = grounding(run(hyps=[h], evidence=evidence, facts=["ev.c#0"]))

    assert g.cited == 6
    assert g.existing == 5  # ev.x#9 is unknown
    assert g.available == 4  # ev.m is a failed result
    assert g.supporting_records == 3  # two records and one finding resolving to a record
    assert g.lexically_supporting == 2  # the heartbeat record shares no content word


def test_key_evidence_recall():
    evidence = {"ev.c#0": record({"change_id": "CHG-E01-2"}), "ev.l#0": record({"message": "Connection pool saturated"})}
    full = score(run(hyps=[hyp("pool size reduced", refs=["ev.c#0", "ev.l#0"])], evidence=evidence), LABELS["EVAL-01"])
    half = score(run(hyps=[hyp("pool size reduced", refs=["ev.c#0"])], evidence=evidence), LABELS["EVAL-01"])

    assert full.key_evidence_recall == 1.0 and half.key_evidence_recall == 0.5


def test_aggregate_includes_failures_and_reports_missing_tokens_as_unknown():
    runs = [run("EVAL-01", hyps=[hyp("pool size reduced")]), run("EVAL-02", status="failed"),
            run("EVAL-09", conclusion="inconclusive")]
    scores = [score(r, LABELS[r.case_id]) for r in runs]
    agg = aggregate(runs, scores)

    assert agg["cases"] == 3
    assert agg["strict_accuracy"] == 0.667 and agg["failure_rate"] == 0.333
    assert agg["outcomes"]["failed"] == 1
    assert agg["tokens"] is None

    with_tokens = [run("EVAL-01", tokens=(100, 10)), run("EVAL-09", tokens=(50, 5))]
    agg2 = aggregate(with_tokens, [score(r, LABELS[r.case_id]) for r in with_tokens])
    assert agg2["tokens"]["input_total"] == 150 and agg2["tokens"]["output_mean"] == 7.5
