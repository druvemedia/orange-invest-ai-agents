from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.evidence import (
    Completeness,
    DependencyRecord,
    EvidenceResult,
    EvidenceSource,
    EvidenceStatus,
    HistoricalIncidentRecord,
    LogRecord,
    MetricRecord,
    ToolError,
    ToolErrorCode,
)
from app.schemas.incident import IncidentReport
from app.schemas.queries import DependencyQuery, LogSearchQuery, MetricQuery

T0 = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


# --- IncidentReport ------------------------------------------------------------


def test_incident_report_accepts_minimal_incomplete_report():
    report = IncidentReport(incident_id="INC-1", summary="Something is slow")

    assert report.affected_services == []
    assert report.severity is None
    assert report.reported_at is None


def test_incident_report_normalises_and_dedupes_services():
    report = IncidentReport(
        incident_id="INC-1", summary="x", affected_services=["Payments-API", " payments-api ", "orders-service"]
    )

    assert report.affected_services == ["payments-api", "orders-service"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": "   "},
        {"incident_id": ""},
        {"severity": "sev0"},
        {"reported_at": "2026-10-20T14:00:00"},  # naive timestamp
        {"unexpected_field": "x"},
    ],
)
def test_incident_report_rejects_invalid_input(overrides):
    data = {"incident_id": "INC-1", "summary": "Checkout slow", **overrides}

    with pytest.raises(ValidationError):
        IncidentReport(**data)


# --- Records -------------------------------------------------------------------


def test_log_record_rejects_unknown_level():
    with pytest.raises(ValidationError):
        LogRecord(timestamp=T0, service="a", level="VERBOSE", message="m")


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_metric_record_rejects_non_finite_values(value):
    with pytest.raises(ValidationError):
        MetricRecord(timestamp=T0, service="a", metric_name="m", value=value, unit="ms")


def test_dependency_record_rejects_self_dependency():
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        DependencyRecord(service="a", depends_on="A", dependency_type="http", critical=True)


def test_historical_incident_rejects_resolution_before_occurrence():
    with pytest.raises(ValidationError, match="resolved_at"):
        HistoricalIncidentRecord(incident_id="H-1", title="t", occurred_at=T1, resolved_at=T0, services=["a"])


def test_historical_incident_allows_unknown_root_cause():
    record = HistoricalIncidentRecord(incident_id="H-1", title="t", occurred_at=T0, services=["a"])

    assert record.root_cause is None


# --- Queries -------------------------------------------------------------------


def test_query_rejects_end_before_start():
    with pytest.raises(ValidationError, match="end must be after start"):
        LogSearchQuery(start=T1, end=T0)


def test_query_rejects_naive_timestamps():
    with pytest.raises(ValidationError):
        MetricQuery(start=datetime(2026, 10, 20, 14), end=datetime(2026, 10, 20, 15))


@pytest.mark.parametrize("limit", [0, 501])
def test_query_rejects_out_of_range_limit(limit):
    with pytest.raises(ValidationError):
        LogSearchQuery(start=T0, end=T1, limit=limit)


def test_query_rejects_unknown_filter():
    with pytest.raises(ValidationError):
        LogSearchQuery(start=T0, end=T1, servce=["payments-api"])


def test_dependency_query_bounds_depth():
    with pytest.raises(ValidationError):
        DependencyQuery(service="a", depth=4)


# --- EvidenceResult ------------------------------------------------------------


def _error() -> ToolError:
    return ToolError(code=ToolErrorCode.TIMEOUT, message="t", retryable=True)


@pytest.mark.parametrize(
    ("completeness", "records", "expected"),
    [
        (Completeness.COMPLETE, [{"timestamp": T0, "service": "a", "level": "INFO", "message": "m"}], EvidenceStatus.OK),
        (Completeness.COMPLETE, [], EvidenceStatus.NO_MATCHES),
        (Completeness.PARTIAL, [], EvidenceStatus.PARTIAL),
    ],
)
def test_evidence_result_status_is_derived(completeness, records, expected):
    result = EvidenceResult[LogRecord](
        source=EvidenceSource.LOGS, query={}, records=records, completeness=completeness
    )

    assert result.status is expected
    assert result.model_dump()["status"] == expected


def test_failed_result_requires_an_error():
    with pytest.raises(ValidationError, match="at least one error"):
        EvidenceResult[LogRecord](source=EvidenceSource.LOGS, query={}, completeness=Completeness.NONE)


def test_failed_result_cannot_carry_records():
    with pytest.raises(ValidationError, match="cannot contain records"):
        EvidenceResult[LogRecord](
            source=EvidenceSource.LOGS,
            query={},
            records=[LogRecord(timestamp=T0, service="a", level="INFO", message="m")],
            completeness=Completeness.NONE,
            errors=[_error()],
        )


def test_complete_result_cannot_be_truncated_or_carry_errors():
    with pytest.raises(ValidationError, match="truncated"):
        EvidenceResult[LogRecord](
            source=EvidenceSource.LOGS, query={}, completeness=Completeness.COMPLETE, truncated=True
        )
    with pytest.raises(ValidationError, match="cannot contain errors"):
        EvidenceResult[LogRecord](
            source=EvidenceSource.LOGS, query={}, completeness=Completeness.COMPLETE, errors=[_error()]
        )
