"""Request and response bodies of the investigations API."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from app.schemas.common import Severity, StrictModel

# Bounds keep prompts (and therefore cost and latency) bounded.
MAX_SUMMARY = 2000
MAX_CONTEXT = 5000

# Stricter than the shared ServiceName type: user input must look like a service identifier.
ApiServiceName = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
]


class InvestigationRequest(StrictModel):
    """An incident to investigate. Only summary and reported_at are required."""

    incident_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")] | None = None
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUMMARY)]
    # Needed to scope the evidence window; must be timezone-aware (e.g. "2026-10-20T14:12:00Z").
    reported_at: AwareDatetime
    affected_services: list[ApiServiceName] = Field(default_factory=list, max_length=10)
    severity: Severity | None = None
    reporter: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    context: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_CONTEXT)] | None = None
    # "demo" or an evaluation world such as "EVAL-07"; see GET /evidence-sources.
    evidence_source: str = "demo"


class Links(StrictModel):
    self: str
    report: str
    report_markdown: str


class Outcome(StrictModel):
    investigation_status: str
    termination_reason: str | None
    conclusion: str
    top_hypothesis: str | None
    report_generated_by: str


class Usage(StrictModel):
    model_calls: int
    input_tokens: int | None  # None when the client reports no usage (e.g. the offline heuristic)
    output_tokens: int | None


class JobView(StrictModel):
    investigation_id: str
    status: Literal["queued", "running", "completed", "failed"]
    incident_id: str
    evidence_source: str
    model: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    outcome: Outcome | None
    usage: Usage | None
    links: Links


class EvidenceSourceView(StrictModel):
    name: str
    description: str
    simulated_faults: dict[str, str]
    example_incidents: list[dict]


class ServiceInfo(StrictModel):
    investigations_available: bool
    model: str
    unavailable_reason: str | None
    max_active_investigations: int
