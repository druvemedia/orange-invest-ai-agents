"""Input schemas for the evidence tools.

Every list filter treats an empty list as "no filter". All timestamps must be
timezone-aware, and time windows are inclusive at both ends.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import NonEmptyStr, ServiceName, Severity, StrictModel, TimeRange
from app.schemas.evidence import ChangeType, LogLevel


class EvidenceQuery(StrictModel):
    limit: int = Field(default=100, ge=1, le=500)


class WindowedQuery(EvidenceQuery):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _check_window(self) -> "WindowedQuery":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    @property
    def window(self) -> TimeRange:
        return TimeRange(start=self.start, end=self.end)


class LogSearchQuery(WindowedQuery):
    services: list[ServiceName] = Field(default_factory=list)
    min_level: LogLevel = LogLevel.DEBUG
    # Case-insensitive substrings; a record matches if its message contains ANY keyword.
    keywords: list[NonEmptyStr] = Field(default_factory=list)
    trace_id: NonEmptyStr | None = None


class MetricQuery(WindowedQuery):
    services: list[ServiceName] = Field(default_factory=list)
    metric_names: list[NonEmptyStr] = Field(default_factory=list)
    limit: int = Field(default=500, ge=1, le=2000)


class ChangeHistoryQuery(WindowedQuery):
    services: list[ServiceName] = Field(default_factory=list)
    change_types: list[ChangeType] = Field(default_factory=list)


class DependencyQuery(EvidenceQuery):
    service: ServiceName
    # downstream = what ``service`` depends on; upstream = what depends on ``service``.
    direction: Literal["downstream", "upstream", "both"] = "both"
    # 1 returns direct edges only; higher values follow edges transitively.
    depth: int = Field(default=1, ge=1, le=3)


class HistoricalIncidentQuery(EvidenceQuery):
    # Case-insensitive substrings matched against title, symptoms, tags, root cause and resolution.
    keywords: list[NonEmptyStr] = Field(default_factory=list)
    services: list[ServiceName] = Field(default_factory=list)
    severities: list[Severity] = Field(default_factory=list)
    occurred_after: AwareDatetime | None = None
    occurred_before: AwareDatetime | None = None
    limit: int = Field(default=10, ge=1, le=50)
