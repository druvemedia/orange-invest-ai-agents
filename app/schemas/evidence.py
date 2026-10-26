"""Evidence records returned by the tools, and the result envelope that wraps them.

Records are raw observations. They carry no interpretation: a tool never states
a root cause, and an empty result never means "this did not happen".
"""

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import AwareDatetime, Field, computed_field, model_validator

from app.schemas.common import NonEmptyStr, ServiceName, Severity, StrictModel


class EvidenceSource(StrEnum):
    LOGS = "logs"
    METRICS = "metrics"
    CHANGES = "changes"
    DEPENDENCIES = "dependencies"
    HISTORICAL_INCIDENTS = "historical_incidents"


# --- Records -----------------------------------------------------------------


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return list(LogLevel).index(self)


class LogRecord(StrictModel):
    timestamp: AwareDatetime
    service: ServiceName
    level: LogLevel
    message: NonEmptyStr
    trace_id: str | None = None


class MetricRecord(StrictModel):
    """A single metric sample, e.g. ``latency_p95_ms`` for one service at one time."""

    timestamp: AwareDatetime
    service: ServiceName
    metric_name: NonEmptyStr
    value: float = Field(allow_inf_nan=False)
    unit: NonEmptyStr


class ChangeType(StrEnum):
    DEPLOYMENT = "deployment"
    CONFIG_CHANGE = "config_change"
    FEATURE_FLAG = "feature_flag"
    ROLLBACK = "rollback"


class ChangeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    ROLLED_BACK = "rolled_back"


class ChangeRecord(StrictModel):
    """One entry of deployment or configuration history."""

    change_id: NonEmptyStr
    timestamp: AwareDatetime
    service: ServiceName
    change_type: ChangeType
    status: ChangeStatus
    summary: NonEmptyStr
    version: str | None = None
    previous_version: str | None = None
    changed_by: str | None = None
    # Structured change details, e.g. {"key": "db.pool.max_size", "old": "50", "new": "20"}.
    details: dict[str, str] = Field(default_factory=dict)


class DependencyType(StrEnum):
    HTTP = "http"
    GRPC = "grpc"
    DATABASE = "database"
    CACHE = "cache"
    QUEUE = "queue"
    EXTERNAL_API = "external_api"


class DependencyRecord(StrictModel):
    """A directed edge: ``service`` calls or relies on ``depends_on``."""

    service: ServiceName
    depends_on: ServiceName
    dependency_type: DependencyType
    critical: bool
    notes: str | None = None

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "DependencyRecord":
        if self.service == self.depends_on:
            raise ValueError("a service cannot depend on itself")
        return self


class HistoricalIncidentRecord(StrictModel):
    """A past, closed incident. ``root_cause`` is what that postmortem recorded, if anything."""

    incident_id: NonEmptyStr
    title: NonEmptyStr
    occurred_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    services: list[ServiceName] = Field(min_length=1)
    severity: Severity | None = None
    symptoms: list[NonEmptyStr] = Field(default_factory=list)
    root_cause: str | None = None
    resolution: str | None = None
    tags: list[NonEmptyStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolved_after_occurred(self) -> "HistoricalIncidentRecord":
        if self.resolved_at is not None and self.resolved_at < self.occurred_at:
            raise ValueError("resolved_at cannot be before occurred_at")
        return self


class CoverageGap(StrictModel):
    """A known window in which a source has no data, so missing records are expected.

    ``service=None`` means the gap applies to every service in that source.
    """

    source: EvidenceSource
    service: ServiceName | None = None
    start: AwareDatetime
    end: AwareDatetime
    reason: NonEmptyStr


# --- Result envelope -----------------------------------------------------------


class Completeness(StrEnum):
    # Every relevant record in the available data was searched and returned.
    COMPLETE = "complete"
    # Some relevant data was missing, unreadable or truncated; see ``warnings``.
    PARTIAL = "partial"
    # The tool failed and obtained no evidence; see ``errors``.
    NONE = "none"


class EvidenceStatus(StrEnum):
    OK = "ok"
    NO_MATCHES = "no_matches"
    PARTIAL = "partial"
    FAILED = "failed"


class ToolErrorCode(StrEnum):
    INVALID_QUERY = "invalid_query"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DATA_UNAVAILABLE = "data_unavailable"


class ToolError(StrictModel):
    code: ToolErrorCode
    message: NonEmptyStr
    retryable: bool
    # True when the failure was injected for testing rather than caused by a real fault.
    simulated: bool = False


RecordT = TypeVar("RecordT", bound=StrictModel)


class EvidenceResult(StrictModel, Generic[RecordT]):
    """What every evidence tool returns, whether it succeeded, partly succeeded or failed."""

    source: EvidenceSource
    query: dict[str, Any]
    records: list[RecordT] = Field(default_factory=list)
    completeness: Completeness
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[ToolError] = Field(default_factory=list)

    @computed_field
    @property
    def record_count(self) -> int:
        return len(self.records)

    @computed_field
    @property
    def status(self) -> EvidenceStatus:
        if self.completeness is Completeness.NONE:
            return EvidenceStatus.FAILED
        if self.completeness is Completeness.PARTIAL:
            return EvidenceStatus.PARTIAL
        if not self.records:
            return EvidenceStatus.NO_MATCHES
        return EvidenceStatus.OK

    @model_validator(mode="after")
    def _check_consistency(self) -> "EvidenceResult[RecordT]":
        if self.completeness is Completeness.NONE:
            if not self.errors:
                raise ValueError("a failed result must include at least one error")
            if self.records:
                raise ValueError("a failed result cannot contain records")
        elif self.completeness is Completeness.COMPLETE:
            if self.errors:
                raise ValueError("a complete result cannot contain errors")
            if self.truncated:
                raise ValueError("a truncated result cannot be complete")
        return self
