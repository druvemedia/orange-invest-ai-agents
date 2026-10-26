from pydantic import AwareDatetime, Field, field_validator

from app.schemas.common import NonEmptyStr, ServiceName, Severity, StrictModel


class IncidentReport(StrictModel):
    """An incident as it was reported, which may be incomplete.

    Only the ID and summary are required: real reports often arrive without a
    severity, affected services or an exact time, and that must be representable.
    ``None`` means "not provided", never "not applicable".
    """

    incident_id: NonEmptyStr
    summary: NonEmptyStr
    affected_services: list[ServiceName] = Field(default_factory=list)
    severity: Severity | None = None
    reported_at: AwareDatetime | None = None
    reporter: NonEmptyStr | None = None
    # Free-text context from the reporter: symptoms, customer impact, suspected start time, etc.
    context: str | None = None

    @field_validator("affected_services")
    @classmethod
    def _dedupe_services(cls, services: list[str]) -> list[str]:
        return list(dict.fromkeys(services))
