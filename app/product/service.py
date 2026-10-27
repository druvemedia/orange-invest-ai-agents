"""Background investigation jobs, kept in memory.

An investigation takes about 100 s with a real model, so the API starts it as a job and returns at
once. Bounds: at most ``max_active`` jobs at a time (more are refused, not queued), a hard time limit
per job on top of the engine's own deadline, and at most ``max_stored`` jobs kept (oldest finished
ones are dropped first). State is lost on restart; there is no database by design.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from app.agents import AgentConfig, OrchestratorAgent
from app.core.redaction import redact
from app.llm import LLMClient
from app.product.report_view import InvestigationReportView, build_view
from app.product.sources import EvidenceSource
from app.schemas.incident import IncidentReport

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "completed", "failed"]
HARD_LIMIT_MARGIN_SECONDS = 60.0


class ServiceBusyError(RuntimeError):
    pass


class ModelUnavailableError(RuntimeError):
    """No usable model is configured. The message names settings, never their values."""


class UsageCounter:
    """Wraps the model client to count calls; token totals come from the client if it reports them."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.model_calls = 0

    async def generate(self, request, output_type):
        self.model_calls += 1
        return await self.inner.generate(request, output_type)

    def summary(self) -> dict:
        stats = [c for c in getattr(self.inner, "calls", []) if getattr(c, "input_tokens", None) is not None]
        return {
            "model_calls": self.model_calls,
            "input_tokens": sum(c.input_tokens for c in stats) if stats else None,
            "output_tokens": sum(c.output_tokens or 0 for c in stats) if stats else None,
        }


class Job:
    def __init__(self, job_id: str, incident: IncidentReport, source: EvidenceSource) -> None:
        self.id = job_id
        self.incident = incident
        self.source = source
        self.status: JobStatus = "queued"
        self.created_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error: str | None = None
        self.view: InvestigationReportView | None = None
        self.usage: dict | None = None
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None


class InvestigationService:
    def __init__(
        self,
        llm_factory: Callable[[], LLMClient] | None,
        *,
        model_label: str,
        agent_config: AgentConfig | None,
        unavailable_reason: str | None = None,
        max_active: int = 2,
        max_stored: int = 50,
    ) -> None:
        self._llm_factory = llm_factory
        self.model_label = model_label
        self._config = agent_config
        self.unavailable_reason = unavailable_reason
        self.max_active = max_active
        self.max_stored = max_stored
        self._jobs: OrderedDict[str, Job] = OrderedDict()

    @property
    def available(self) -> bool:
        return self._llm_factory is not None and self._config is not None and self.unavailable_reason is None

    def submit(self, incident: IncidentReport, source: EvidenceSource) -> Job:
        if not self.available:
            raise ModelUnavailableError(self.unavailable_reason or "No model is configured.")
        if sum(1 for j in self._jobs.values() if j.status in ("queued", "running")) >= self.max_active:
            raise ServiceBusyError(f"{self.max_active} investigation(s) already running; try again later.")
        job = Job(f"inv-{uuid4().hex[:12]}", incident, source)
        self._jobs[job.id] = job
        self._evict()
        job.task = asyncio.get_running_loop().create_task(self._run(job), name=f"investigation:{job.id}")
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return list(reversed(self._jobs.values()))

    async def wait(self, job_id: str) -> Job:
        job = self._jobs[job_id]
        await job.done.wait()
        return job

    async def shutdown(self) -> None:
        tasks = [j.task for j in self._jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: Job) -> None:
        job.status, job.started_at = "running", datetime.now(UTC)
        hard_limit = self._config.limits.deadline_seconds + HARD_LIMIT_MARGIN_SECONDS
        llm: UsageCounter | None = None
        try:
            llm = UsageCounter(self._llm_factory())
            orchestrator = OrchestratorAgent(llm, job.source.tools(), self._config)
            async with asyncio.timeout(hard_limit):
                result = await orchestrator.investigate(job.incident, job.id)
            job.view = build_view(result, evidence_source=job.source.name, model=self.model_label)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status, job.error = "failed", "The investigation was cancelled (server shutting down)."
            raise
        except TimeoutError:
            job.status, job.error = "failed", f"The investigation exceeded the hard time limit of {hard_limit:g}s."
        except Exception as exc:  # never let a job crash silently or leak details
            job.status = "failed"
            job.error = redact(f"Internal error ({type(exc).__name__}).")
            # Type only: a traceback would include the exception message, which may contain secrets.
            logger.error("investigation=%s failed: %s", job.id, type(exc).__name__)
        finally:
            job.usage = llm.summary() if llm else None
            job.finished_at = datetime.now(UTC)
            job.done.set()
            logger.info("investigation=%s status=%s", job.id, job.status)

    def _evict(self) -> None:
        while len(self._jobs) > self.max_stored:
            oldest_done = next((jid for jid, j in self._jobs.items() if j.done.is_set()), None)
            if oldest_done is None:
                break
            del self._jobs[oldest_done]
