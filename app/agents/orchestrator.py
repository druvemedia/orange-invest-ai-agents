"""Orchestrator agent: scopes the incident, builds the task graph and runs it on the engine.

The model proposes a scope (time window, focus services, keywords); code validates and bounds
it, then builds the plan deterministically. Every budget is set here: engine limits, critic
rounds, evidence requests per round, attempts and timeouts per task.

Initial plan:    orchestrate  ──follow-ups──►  evidence tasks, agents, reasoning.r1, critic.r1
                 report (finalizer: runs last, whatever happened)
Critic loop:     critic.rN ──follow-ups──► ev.cN.* , reasoning.r(N+1), critic.r(N+1)   while N < max rounds
"""

import re
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import LLMAgent, Prepared
from app.agents.contracts import (
    AgentEnvelope,
    FinalReport,
    InvestigationScope,
    ScopeInput,
    ScopeOutput,
)
from app.agents.critic import CriticAgent
from app.agents.dependency import DependencyAgent
from app.agents.evidence import RefIndex
from app.agents.observability import ObservabilityAgent
from app.agents.reasoning import ReasoningAgent
from app.agents.reporting import ReportingAgent, build_fallback_report
from app.core.config import Settings
from app.llm import LLMClient
from app.orchestration import (
    EVIDENCE_TASK_KIND,
    DependencyPolicy,
    EngineLimits,
    EvidenceTaskHandler,
    Investigation,
    InvestigationEngine,
    InvestigationPlan,
    RetryPolicy,
    TaskContext,
    TaskError,
    TaskErrorCode,
    TaskFailure,
    TaskSpec,
    TaskStatus,
    evidence_task,
)
from app.schemas.common import StrictModel
from app.schemas.incident import IncidentReport
from app.tools.base import EvidenceTool

SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
MAX_FOCUS_SERVICES = 4
MAX_KEYWORDS = 6


# Each agent attempt makes one model request. The engine's per-attempt timeout is the provider
# timeout plus this margin, so a slow request is normally ended by the SDK timeout and recorded as
# a retryable model timeout. The engine timeout is only a backstop.
AGENT_TIMEOUT_MARGIN_SECONDS = 30.0


class AgentConfig(StrictModel):
    max_critic_rounds: int = Field(default=2, ge=1, le=5)
    max_evidence_requests: int = Field(default=3, ge=0, le=5)
    agent_timeout_seconds: float = Field(default=120.0 + AGENT_TIMEOUT_MARGIN_SECONDS, gt=0, le=900)
    agent_retry: RetryPolicy = RetryPolicy(max_attempts=2)
    evidence_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    evidence_retry: RetryPolicy = RetryPolicy(max_attempts=2)
    max_window_hours: float = Field(default=6.0, gt=0, le=48)
    change_lookback_hours: float = Field(default=24.0, ge=0, le=168)
    limits: EngineLimits = EngineLimits(max_tasks=40, max_total_attempts=80, max_concurrency=5, deadline_seconds=300)

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentConfig":
        """Build limits from settings. Raises ValueError if the deadline cannot fit one agent attempt."""
        agent_timeout = settings.llm_timeout_seconds + AGENT_TIMEOUT_MARGIN_SECONDS
        if settings.investigation_deadline_seconds < agent_timeout:
            raise ValueError(
                f"INVESTIGATION_DEADLINE_SECONDS ({settings.investigation_deadline_seconds:g}) must be at least "
                f"LLM_TIMEOUT_SECONDS + {AGENT_TIMEOUT_MARGIN_SECONDS:g} ({agent_timeout:g}), "
                "or a single model request could never finish before the deadline."
            )
        return cls(
            max_critic_rounds=settings.agent_max_critic_rounds,
            max_evidence_requests=settings.agent_max_evidence_requests,
            agent_timeout_seconds=agent_timeout,
            limits=EngineLimits(
                max_tasks=40,
                max_total_attempts=80,
                max_concurrency=5,
                deadline_seconds=settings.investigation_deadline_seconds,
            ),
        )


class InvestigationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation: Investigation
    report: FinalReport


class OrchestratorAgent(LLMAgent[ScopeInput, ScopeOutput, InvestigationScope]):
    name = "orchestrator"
    kind = "agent.orchestrator"
    output_model = ScopeOutput
    instructions = """
You are the Orchestrator of an incident investigation team. Decide the investigation scope from the
incident report, which may be incomplete:
- window_start / window_end: the period in which the incident most likely started and developed
  (timezone-aware ISO 8601). Keep it within max_window_hours, and make sure it covers the reported time.
- focus_services: services the report names or clearly implies (lower-case names). Empty if unknown.
- keywords: up to 6 short search terms describing the symptoms.
- rationale: one or two sentences. Do not speculate about causes.
""".strip()

    def __init__(self, llm: LLMClient, tools: dict[str, EvidenceTool], config: AgentConfig | None = None) -> None:
        super().__init__(llm)
        self.config = config or AgentConfig()
        self.tools = tools
        agents = [
            self,
            ObservabilityAgent(llm),
            DependencyAgent(llm),
            ReasoningAgent(llm),
            CriticAgent(llm, tools),
            ReportingAgent(llm),
        ]
        handlers = {agent.kind: agent for agent in agents}
        handlers[EVIDENCE_TASK_KIND] = EvidenceTaskHandler(tools)
        self.engine = InvestigationEngine(handlers, self.config.limits)

    # --- Coordination -------------------------------------------------------------------------

    def initial_plan(self) -> InvestigationPlan:
        return InvestigationPlan(
            tasks=[
                self._agent_task("orchestrate", self.kind, required=True),
                self._agent_task("report", ReportingAgent.kind, required=True, finalizer=True),
            ]
        )

    async def investigate(self, incident: IncidentReport, investigation_id: str | None = None) -> InvestigationResult:
        investigation = await self.engine.run(incident, self.initial_plan(), investigation_id)
        report_task = investigation.tasks["report"]
        if report_task.status is TaskStatus.SUCCEEDED:
            envelope = AgentEnvelope.model_validate(report_task.output.data)
            report = FinalReport.model_validate(envelope.result).model_copy(
                update={"adjustments": envelope.adjustments, "generated_by": envelope.generated_by}
            )
        else:
            report = build_fallback_report(
                incident,
                investigation.investigation_id,
                investigation.tasks,
                f"the reporting step did not run ({investigation.termination_reason})",
            )
        return InvestigationResult(investigation=investigation, report=report)

    # --- Scoping (engine handler for the "orchestrate" task) ----------------------------------

    def prepare(self, ctx: TaskContext) -> Prepared[ScopeInput]:
        return Prepared(
            input=ScopeInput(incident=ctx.incident, max_window_hours=self.config.max_window_hours),
            refs=RefIndex(),
            consumed=[],
        )

    def check(self, output: ScopeOutput, prepared: Prepared[ScopeInput]) -> list[str]:
        return []  # any proposal is bounded and corrected in resolve_scope

    def fallback(self, ctx, prepared, error):
        return default_proposal(ctx.incident)

    def finish(self, ctx, output, prepared):
        scope, adjustments = resolve_scope(output, ctx.incident, self.config)
        return scope, adjustments, self.plan_tasks(scope)

    def plan_tasks(self, scope: InvestigationScope) -> list[TaskSpec]:
        window = {"start": scope.window_start.isoformat(), "end": scope.window_end.isoformat()}
        ev = dict(retry=self.config.evidence_retry, timeout_seconds=self.config.evidence_timeout_seconds)
        evidence = [
            evidence_task("ev.logs", "search_logs", {**window, "min_level": "WARNING", "limit": 200}, **ev),
            evidence_task("ev.metrics", "query_metrics", {**window, "limit": 1000}, **ev),
            evidence_task(
                "ev.changes",
                "get_change_history",
                {"start": scope.change_lookback_start.isoformat(), "end": scope.window_end.isoformat()},
                **ev,
            ),
            *[
                evidence_task(f"ev.deps.{s}", "get_service_dependencies", {"service": s, "direction": "both", "depth": 2}, **ev)
                for s in scope.focus_services
            ],
            evidence_task(
                "ev.history",
                "search_historical_incidents",
                {"keywords": scope.keywords, "services": scope.focus_services, "limit": 5},
                **ev,
            ),
        ]
        ids = [t.task_id for t in evidence]
        deps_ids = [i for i in ids if i.startswith("ev.deps.")]
        reasoning_deps = ["orchestrate", "observability", "dependency", *ids]
        agents = [
            self._agent_task("observability", ObservabilityAgent.kind, ["orchestrate", "ev.logs", "ev.metrics"]),
            self._agent_task("dependency", DependencyAgent.kind, ["orchestrate", "ev.changes", *deps_ids]),
            self._agent_task("reasoning.r1", ReasoningAgent.kind, reasoning_deps, inputs={"round": 1}),
            self._agent_task(
                "critic.r1",
                CriticAgent.kind,
                [*reasoning_deps, "reasoning.r1"],
                inputs={
                    "round": 1,
                    "reviews": "reasoning.r1",
                    "max_rounds": self.config.max_critic_rounds,
                    "max_evidence_requests": self.config.max_evidence_requests,
                    "evidence_retry": self.config.evidence_retry.model_dump(),
                    "evidence_timeout_seconds": self.config.evidence_timeout_seconds,
                },
            ),
        ]
        return evidence + agents

    def _agent_task(self, task_id, kind, depends_on=(), inputs=None, **fields) -> TaskSpec:
        return TaskSpec(
            task_id=task_id,
            kind=kind,
            depends_on=list(depends_on),
            inputs=inputs or {},
            retry=self.config.agent_retry,
            timeout_seconds=self.config.agent_timeout_seconds,
            # Agents run with whatever evidence exists and report the gaps themselves.
            dependency_policy=DependencyPolicy.ALLOW_FAILURES,
            **fields,
        )


def default_proposal(incident: IncidentReport) -> ScopeOutput:
    if incident.reported_at is None:
        raise TaskFailure(
            TaskError(code=TaskErrorCode.INVALID_INPUT, message="Cannot scope an incident without a reported time.")
        )
    return ScopeOutput(
        window_start=incident.reported_at - timedelta(minutes=90),
        window_end=incident.reported_at + timedelta(minutes=45),
        focus_services=list(incident.affected_services),
        keywords=[],
        rationale="Default scope: 90 minutes before to 45 minutes after the reported time.",
    )


def resolve_scope(
    proposal: ScopeOutput, incident: IncidentReport, config: AgentConfig
) -> tuple[InvestigationScope, list[str]]:
    """Bound and correct a proposed scope. Returns the scope and the corrections made."""
    adjustments = []
    start, end = proposal.window_start, proposal.window_end
    span_ok = end > start and (end - start) <= timedelta(hours=config.max_window_hours)
    anchor = incident.reported_at
    covers_report = anchor is None or (start <= anchor + timedelta(hours=2) and end >= anchor - timedelta(hours=12))
    if not (span_ok and covers_report):
        default = default_proposal(incident)
        start, end = default.window_start, default.window_end
        adjustments.append("Proposed time window was invalid, too long or far from the reported time; default used.")

    focus = list(incident.affected_services)
    for s in (s.strip().lower() for s in proposal.focus_services):
        if s not in focus and SERVICE_NAME.match(s) and len(focus) < MAX_FOCUS_SERVICES:
            focus.append(s)
    dropped = [s for s in proposal.focus_services if s.strip().lower() not in focus]
    if dropped:
        adjustments.append(f"Ignored focus services (invalid or over the limit of {MAX_FOCUS_SERVICES}): {dropped}")

    keywords = [k.strip() for k in proposal.keywords if 2 <= len(k.strip()) <= 40][:MAX_KEYWORDS]
    scope = InvestigationScope(
        window_start=start,
        window_end=end,
        change_lookback_start=start - timedelta(hours=config.change_lookback_hours),
        focus_services=focus,
        keywords=list(dict.fromkeys(keywords)),
        rationale=proposal.rationale.strip()[:500] or "No rationale given.",
    )
    return scope, adjustments


def build_orchestrator(llm: LLMClient, settings: Settings, tools: dict[str, EvidenceTool]) -> OrchestratorAgent:
    return OrchestratorAgent(llm, tools, AgentConfig.from_settings(settings))
