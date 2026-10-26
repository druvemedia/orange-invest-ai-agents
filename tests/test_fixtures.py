"""The shipped synthetic fixtures must stay valid, consistent and free of incident answers."""

import pytest

from app.schemas.evidence import (
    ChangeRecord,
    CoverageGap,
    DependencyRecord,
    HistoricalIncidentRecord,
    LogRecord,
    MetricRecord,
)
from app.schemas.incident import IncidentReport
from app.tools import TOOL_CLASSES

DATASETS = [
    ("incidents.jsonl", IncidentReport),
    ("logs.jsonl", LogRecord),
    ("metrics.jsonl", MetricRecord),
    ("changes.jsonl", ChangeRecord),
    ("dependencies.jsonl", DependencyRecord),
    ("historical_incidents.jsonl", HistoricalIncidentRecord),
    ("coverage_gaps.jsonl", CoverageGap),
]


@pytest.mark.parametrize(("name", "model"), DATASETS)
def test_every_fixture_line_is_valid(fixture_store, name, model):
    dataset = fixture_store.load(name, model)

    assert dataset.records
    assert dataset.issues == ()


def test_every_tool_dataset_is_covered():
    assert {cls.dataset for cls in TOOL_CLASSES} <= {name for name, _ in DATASETS}


def test_current_incident_reports_contain_no_conclusions(fixture_store):
    # Reports describe symptoms only; the schema has no root-cause field, and IDs don't collide with history.
    reports = fixture_store.load("incidents.jsonl", IncidentReport).records
    history = fixture_store.load("historical_incidents.jsonl", HistoricalIncidentRecord).records

    assert "root_cause" not in IncidentReport.model_fields
    assert {r.incident_id for r in reports}.isdisjoint({h.incident_id for h in history})


def test_incident_fixtures_include_an_incomplete_report(fixture_store):
    reports = fixture_store.load("incidents.jsonl", IncidentReport).records

    assert any(r.severity is None and not r.affected_services for r in reports)
