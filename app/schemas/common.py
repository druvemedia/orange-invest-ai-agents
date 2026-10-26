"""Shared types and base models for incident and evidence schemas."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Service names are compared case-insensitively, so they are normalised to lower case.
ServiceName = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=100)
]


class StrictModel(BaseModel):
    """Base for domain models: unknown fields are rejected (catches fixture and tool-call typos)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TimeRange(StrictModel):
    """Inclusive time window [start, end]. Both bounds must be timezone-aware."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _check_order(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts <= self.end

    def overlaps(self, other: "TimeRange") -> bool:
        return self.start <= other.end and other.start <= self.end
