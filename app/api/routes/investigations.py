"""Investigations API: submit an incident, poll the job, read the report (JSON or Markdown).

Errors use FastAPI's {"detail": ...} format. Messages are fixed or redacted; they never contain
provider responses, stack traces or configuration values.
"""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.exceptions import HTTPException
from fastapi.responses import PlainTextResponse

from app.product.report_view import InvestigationReportView, render_markdown
from app.product.service import InvestigationService, Job, ModelUnavailableError, ServiceBusyError
from app.product.sources import EvidenceSource
from app.schemas.incident import IncidentReport
from app.schemas.investigation_api import (
    EvidenceSourceView,
    InvestigationRequest,
    JobView,
    Links,
    Outcome,
    ServiceInfo,
)

router = APIRouter(tags=["investigations"])


def _service(request: Request) -> InvestigationService:
    return request.app.state.investigations


def _sources(request: Request) -> dict[str, EvidenceSource]:
    return request.app.state.evidence_sources


def _job_or_404(request: Request, investigation_id: str) -> Job:
    job = _service(request).get(investigation_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Investigation not found.")
    return job


def _view(job: Job, service: InvestigationService) -> JobView:
    outcome = None
    if job.view is not None:
        v = job.view
        outcome = Outcome(
            investigation_status=v.process.investigation_status,
            termination_reason=v.process.termination_reason,
            conclusion=v.conclusion,
            top_hypothesis=v.hypotheses[0].title if v.hypotheses else None,
            report_generated_by=v.process.report_generated_by,
        )
    base = f"/investigations/{job.id}"
    return JobView(
        investigation_id=job.id, status=job.status, incident_id=job.incident.incident_id,
        evidence_source=job.source.name, model=service.model_label, created_at=job.created_at,
        started_at=job.started_at, finished_at=job.finished_at, error=job.error, outcome=outcome, usage=job.usage,
        links=Links(self=base, report=f"{base}/report", report_markdown=f"{base}/report.md"),
    )


@router.get("/capabilities", response_model=ServiceInfo)
def capabilities(request: Request) -> ServiceInfo:
    service = _service(request)
    return ServiceInfo(
        investigations_available=service.available,
        model=service.model_label,
        unavailable_reason=service.unavailable_reason,
        max_active_investigations=service.max_active,
    )


@router.get("/evidence-sources", response_model=list[EvidenceSourceView])
def evidence_sources(request: Request) -> list[EvidenceSourceView]:
    return [
        EvidenceSourceView(
            name=s.name, description=s.description,
            simulated_faults={tool: str(fault) for tool, fault in s.faults.items()},
            example_incidents=[e.model_dump(mode="json") for e in s.examples],
        )
        for s in _sources(request).values()
    ]


@router.post("/investigations", response_model=JobView, status_code=status.HTTP_202_ACCEPTED,
             responses={422: {"description": "Invalid incident"}, 429: {"description": "Too many running investigations"},
                        503: {"description": "No model configured"}})
async def submit(body: InvestigationRequest, request: Request, response: Response) -> JobView:
    source = _sources(request).get(body.evidence_source)
    if source is None:
        raise HTTPException(
            422,  # same status FastAPI uses for body validation errors
            [{"loc": ["body", "evidence_source"], "type": "unknown_evidence_source",
              "msg": f"Unknown evidence source. Use one of: {', '.join(_sources(request))}."}],
        )
    incident = IncidentReport(
        incident_id=body.incident_id or f"INC-{uuid4().hex[:8].upper()}",
        summary=body.summary, affected_services=body.affected_services, severity=body.severity,
        reported_at=body.reported_at, reporter=body.reporter, context=body.context,
    )
    service = _service(request)
    try:
        job = service.submit(incident, source)
    except ModelUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Investigations are unavailable: {exc}") from None
    except ServiceBusyError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc), headers={"Retry-After": "30"}) from None
    response.headers["Location"] = f"/investigations/{job.id}"
    return _view(job, service)


@router.get("/investigations", response_model=list[JobView])
def list_investigations(request: Request) -> list[JobView]:
    service = _service(request)
    return [_view(j, service) for j in service.list()]


@router.get("/investigations/{investigation_id}", response_model=JobView)
def get_investigation(investigation_id: str, request: Request) -> JobView:
    return _view(_job_or_404(request, investigation_id), _service(request))


def _finished_view(request: Request, investigation_id: str) -> InvestigationReportView:
    job = _job_or_404(request, investigation_id)
    if job.status == "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"The investigation failed: {job.error}")
    if job.view is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"The investigation is still {job.status}; poll {job.id} and retry.")
    return job.view


@router.get("/investigations/{investigation_id}/report", response_model=InvestigationReportView)
def get_report(investigation_id: str, request: Request,
               format: Annotated[str, Query(pattern="^(json|markdown)$")] = "json"):
    view = _finished_view(request, investigation_id)
    if format == "markdown":
        return PlainTextResponse(render_markdown(view), media_type="text/markdown; charset=utf-8")
    return view


@router.get("/investigations/{investigation_id}/report.md", response_class=PlainTextResponse)
def get_report_markdown(investigation_id: str, request: Request) -> PlainTextResponse:
    return PlainTextResponse(render_markdown(_finished_view(request, investigation_id)),
                             media_type="text/markdown; charset=utf-8")
