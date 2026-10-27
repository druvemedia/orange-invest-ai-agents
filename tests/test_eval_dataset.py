"""The labelled evaluation dataset: valid, reproducible, covering every scenario, and labels kept apart."""

import json

import pytest

from app.evaluation import dataset_builder
from app.evaluation.dataset import EVAL_DIR, GoldLabel, load_cases, load_labels
from app.schemas.evidence import (
    ChangeRecord,
    CoverageGap,
    DependencyRecord,
    HistoricalIncidentRecord,
    LogRecord,
    MetricRecord,
)
from app.tools import TOOL_CLASSES, FixtureStore

CASES = load_cases()
LABELS = load_labels()
REQUIRED_SCENARIOS = {
    "config_change_precedes_incident",
    "misleading_deployment_correlation",
    "conflicting_logs_and_metrics",
    "missing_or_delayed_metrics",
    "dependency_failure",
    "retry_or_timeout_problem",
    "simulated_tool_failure",
    "multiple_plausible_hypotheses",
    "insufficient_evidence",
    "strong_hypothesis_contradicted",
}
DATASETS = [("logs.jsonl", LogRecord), ("metrics.jsonl", MetricRecord), ("changes.jsonl", ChangeRecord),
            ("dependencies.jsonl", DependencyRecord), ("historical_incidents.jsonl", HistoricalIncidentRecord),
            ("coverage_gaps.jsonl", CoverageGap)]


def test_at_least_ten_cases_each_with_exactly_one_label():
    assert len(CASES) >= 10
    assert [c.case_id for c in CASES] == sorted(LABELS)


def test_every_required_scenario_is_covered():
    covered = {tag for label in LABELS.values() for tag in label.scenario}
    assert REQUIRED_SCENARIOS <= covered


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_case_evidence_is_valid(case):
    store = FixtureStore(case.data_dir)
    for name, model in DATASETS:
        assert store.load(name, model).issues == (), name
    assert case.incident.incident_id == case.case_id
    assert case.incident.reported_at is not None
    assert set(case.conditions.faults) <= {cls.name for cls in TOOL_CLASSES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_key_evidence_in_labels_exists_in_the_case_data(case):
    """Labels must be grounded: every key-evidence matcher matches a record the tools can return."""
    label = LABELS[case.case_id]
    if label.root_cause is None:
        return
    store = FixtureStore(case.data_dir)
    records = [r.model_dump(mode="json") for name, model in DATASETS[:3] for r in store.load(name, model).records]
    for m in label.root_cause.key_evidence:
        assert any(
            (m.equals is not None and str(r.get(m.field)) == m.equals)
            or (m.contains is not None and m.contains.lower() in str(r.get(m.field, "")).lower())
            for r in records
        ), (case.case_id, m)


def test_labels_are_outside_case_directories_and_not_in_case_files():
    labels_path = (EVAL_DIR / "labels.jsonl").resolve()
    for case in CASES:
        assert not labels_path.is_relative_to(case.data_dir.resolve())
        contents = " ".join(p.read_text(encoding="utf-8") for p in case.data_dir.iterdir())
        label = LABELS[case.case_id]
        for secret in [label.rationale, label.root_cause.summary if label.root_cause else None,
                       label.root_cause.label_id if label.root_cause else None, "expected_outcome"]:
            if secret:
                assert secret not in contents, (case.case_id, secret[:40])


def test_inconsistent_labels_are_rejected():
    good = json.loads((EVAL_DIR / "labels.jsonl").read_text().splitlines()[8])  # EVAL-09, expected inconclusive
    with pytest.raises(ValueError):
        GoldLabel.model_validate({**good, "expected_outcome": "root_cause"})  # root cause missing
    with pytest.raises(ValueError):
        GoldLabel.model_validate({**good, "inconclusive_acceptable": False})


def test_committed_dataset_matches_the_builder(tmp_path):
    dataset_builder.build(tmp_path)
    generated = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    committed = sorted(p.relative_to(EVAL_DIR) for p in EVAL_DIR.rglob("*") if p.is_file() and p.name != "README.md")
    assert generated == committed
    for rel in generated:
        assert (tmp_path / rel).read_bytes() == (EVAL_DIR / rel).read_bytes(), rel
