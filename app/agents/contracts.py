"""Typed inputs and outputs of the six agents.

``*Output`` models are what the language model must return. They use plain field types
only (no defaults or length constraints) so they work as strict structured-output schemas.
Every semantic rule (non-empty lists, valid references, confidence limits) is enforced
in code after parsing, by each agent's ``check`` method.

``*Result`` models are what an agent task stores in the investigation. Each combines the
validated model output with facts computed deterministically in code.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from app.agents.evidence import EvidenceSet
from app.agents.grounding import CitationCheck
from app.schemas.common import StrictModel
from app.schemas.incident import IncidentReport

Confidence = Literal["low", "medium", "high"]
CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
GeneratedBy = Literal["model", "fallback", "rules"]
TOOL_NAMES = (
    "search_logs", "query_metrics", "get_change_history", "get_service_dependencies", "search_historical_incidents"
)


class AgentEnvelope(StrictModel):
    """Stored in ``TaskOutput.data`` by every agent task."""

    agent: str
    generated_by: GeneratedBy
    consumed_tasks: list[str]
    # Changes code made to the model output (e.g. confidence capped), with reasons.
    adjustments: list[str]
    result: dict[str, Any]


# --- Orchestrator --------------------------------------------------------------------------


class ScopeInput(StrictModel):
    incident: IncidentReport
    max_window_hours: float


class ScopeOutput(StrictModel):
    window_start: AwareDatetime
    window_end: AwareDatetime
    focus_services: list[str]
    keywords: list[str]
    rationale: str


class InvestigationScope(StrictModel):
    window_start: datetime
    window_end: datetime
    change_lookback_start: datetime
    focus_services: list[str]
    keywords: list[str]
    rationale: str


# --- Observability --------------------------------------------------------------------------


class Observation(StrictModel):
    observation_id: str  # "obs-1", "obs-2", ...
    kind: Literal["fact", "interpretation"]
    statement: str
    services: list[str]
    evidence_refs: list[str]


class MetricSummary(StrictModel):
    """Computed in code from raw samples; no judgement applied."""

    service: str
    metric_name: str
    unit: str
    samples: int
    baseline: float | None  # median of the samples before the estimated onset; None if there are too few
    baseline_basis: str  # which samples the baseline uses, or why there is none (see app.agents.evidence)
    minimum: float
    peak: float
    peak_at: datetime
    last: float
    first_ref: str
    peak_ref: str
    last_ref: str


class ObservabilityInput(StrictModel):
    incident: IncidentReport
    scope: InvestigationScope
    evidence: list[EvidenceSet]
    metric_summaries: list[MetricSummary]


class ObservabilityOutput(StrictModel):
    observations: list[Observation]
    onset_estimate: AwareDatetime | None
    evidence_gaps: list[str]
    summary: str


class ObservabilityResult(StrictModel):
    output: ObservabilityOutput
    metric_summaries: list[MetricSummary]
    data_gaps: list[str]


# --- Dependency ---------------------------------------------------------------------------


ChangeRelationKind = Literal["focus_service", "dependency_of_focus", "depends_on_focus", "no_known_relation", "unknown"]


class ChangeRelation(StrictModel):
    """Computed in code: when a change happened and how its service relates to the focus services.

    Temporal and structural proximity only; it says nothing about causation.
    """

    change_ref: str
    change_id: str
    service: str
    change_type: str
    timestamp: datetime
    minutes_before_report: float | None  # negative if the change happened after the report
    relation: ChangeRelationKind
    path: list[str]  # dependency path from a focus service to the changed service, if any


class DependencyFinding(StrictModel):
    finding_id: str  # "dep-1", ...
    kind: Literal["fact", "interpretation"]
    statement: str
    services: list[str]
    evidence_refs: list[str]


class DependencyInput(StrictModel):
    incident: IncidentReport
    scope: InvestigationScope
    evidence: list[EvidenceSet]
    change_relations: list[ChangeRelation]


class DependencyOutput(StrictModel):
    findings: list[DependencyFinding]
    evidence_gaps: list[str]
    summary: str


class DependencyResult(StrictModel):
    output: DependencyOutput
    change_relations: list[ChangeRelation]
    data_gaps: list[str]


# --- Reasoning --------------------------------------------------------------------------------


class Hypothesis(StrictModel):
    hypothesis_id: str  # "H1", ...
    title: str
    description: str
    supporting_refs: list[str]
    contradicting_refs: list[str]
    missing_information: list[str]
    confidence: Confidence
    confidence_rationale: str


class CritiqueIssue(StrictModel):
    issue_id: str  # "C1", ...
    hypothesis_id: str | None
    issue_type: Literal[
        "unsupported_claim",
        "contradiction",
        "alternative_explanation",
        "missing_evidence",
        "overconfidence",
        "causation_from_correlation",
    ]
    detail: str
    evidence_refs: list[str]


class ReasoningInput(StrictModel):
    incident: IncidentReport
    scope: InvestigationScope
    round: int
    observability: ObservabilityResult | None
    dependency: DependencyResult | None
    evidence: list[EvidenceSet]
    previous_hypotheses: list[Hypothesis]
    critique: list[CritiqueIssue]
    # Evidence tasks added at the critic's request since the previous round.
    new_evidence_refs: list[str]


class ReasoningOutput(StrictModel):
    hypotheses: list[Hypothesis]
    insufficient_evidence: bool
    critique_responses: list[str]
    summary: str


# --- Evidence independence and timeline checks (computed in code) ----------------------------------


class ObservationGroup(StrictModel):
    """Direct evidence records that describe one underlying observation (see app.agents.independence)."""

    refs: list[str]
    primary_source: str
    sources: list[str]
    merged_because: list[str]


TemporalRelation = Literal["precedes_onset", "follows_onset", "ambiguous", "unknown", "persisted_after_rollback"]


class ChangeTiming(StrictModel):
    change_ref: str
    change_id: str
    change_type: str
    change_status: str
    service: str
    time: str | None  # the change record's instant in UTC; None if missing or invalid
    relation: TemporalRelation
    detail: str
    rollback_ref: str | None = None
    persisted_refs: list[str] = Field(default_factory=list)


class TemporalAssessment(StrictModel):
    """Is the hypothesis's proposed change consistent with when symptoms began? (app.agents.temporal)"""

    hypothesis_id: str
    status: Literal["consistent", "change_follows_onset", "persisted_after_rollback", "ordering_ambiguous",
                    "unknown", "not_applicable"]
    onset: str | None  # earliest observed symptom, UTC
    onset_refs: list[str]
    onset_precision_seconds: float | None
    changes: list[ChangeTiming]
    blocks_cause: bool  # true: the cited change(s) cannot explain the onset
    may_have_worsened: bool
    worsening_refs: list[str]
    detail: str


class ReasoningResult(StrictModel):
    round: int
    output: ReasoningOutput
    # Citations code removed from support or moved to contradicting (see app.agents.grounding).
    citation_checks: list[CitationCheck] = Field(default_factory=list)
    temporal_checks: list[TemporalAssessment] = Field(default_factory=list)


# --- Critic -----------------------------------------------------------------------------------


class EvidenceRequest(StrictModel):
    # A plain string, so a request naming an unknown tool is rejected on its own by the Critic
    # instead of invalidating the whole response. The enum in the JSON schema still constrains
    # strict structured output to the registered tools.
    tool: str = Field(json_schema_extra={"enum": list(TOOL_NAMES)})
    # The tool query as a JSON object string; validated against the tool's query schema.
    query_json: str
    reason: str
    hypothesis_id: str | None


class CriticInput(StrictModel):
    incident: IncidentReport
    round: int
    max_rounds: int
    rounds_remaining: int
    max_evidence_requests: int
    hypotheses: list[Hypothesis]
    reasoning_insufficient_evidence: bool
    evidence: list[EvidenceSet]
    findings: list[Observation | DependencyFinding]
    # Problems already found by deterministic grounding checks.
    rule_issues: list[CritiqueIssue]
    available_tools: dict[str, dict[str, Any]]


class CriticOutput(StrictModel):
    verdict: Literal["accept", "revise", "insufficient_evidence"]
    issues: list[CritiqueIssue]
    evidence_requests: list[EvidenceRequest]
    summary: str


LoopDecision = Literal["accepted", "revision_requested", "max_rounds_reached", "nothing_new_to_investigate"]


class RejectedRequest(StrictModel):
    request: EvidenceRequest
    reason: str


class CriticResult(StrictModel):
    round: int
    reviewed_task: str
    output: CriticOutput  # issues include the rule-based ones
    final_verdict: Literal["accept", "revise", "insufficient_evidence"]
    loop_decision: LoopDecision
    accepted_requests: list[str]  # task IDs of follow-up evidence tasks
    rejected_requests: list[RejectedRequest]


# --- Reporting --------------------------------------------------------------------------------


class RankedHypothesis(StrictModel):
    rank: int
    hypothesis_id: str
    title: str
    confidence: Confidence
    supporting_refs: list[str]
    contradicting_refs: list[str]
    assessment: str


class Statement(StrictModel):
    text: str
    evidence_refs: list[str]


class CorrectiveAction(StrictModel):
    action: str
    timing: Literal["immediate", "follow_up", "preventive"]
    hypothesis_id: str | None


# --- Evidence inventory and final adjudication (computed in code) ---------------------------------


InventoryStatus = Literal["retrieved", "partial", "no_matches", "failed", "timed_out", "not_run"]


class InventoryEntry(StrictModel):
    task_id: str
    tool: str
    status: InventoryStatus
    requested_by: str | None
    query: dict[str, Any]
    record_count: int
    signals: list[str]  # what was actually retrieved, e.g. "metrics: payments-api/request_rate_rps"
    detail: str | None  # error or coverage-gap warning


class EvidenceInventory(StrictModel):
    """Authoritative record of what this investigation queried and obtained. Built from tool outcomes."""

    entries: list[InventoryEntry]
    retrieved_signals: list[str]
    unavailable: list[str]  # statements about evidence that genuinely was not obtained
    never_queried_tools: list[str]


class AdjudicationCheck(StrictModel):
    name: str
    passed: bool
    detail: str


class Adjudication(StrictModel):
    """The final decision on whether a cause may be named, with every condition it checked."""

    decision: Literal["cause_accepted", "inconclusive"]
    leading_hypothesis: str | None
    basis: str
    critic_verdict: str | None
    checks: list[AdjudicationCheck]
    blocking_issues: list[str]
    unresolved_critic_issues: list[str]


class ReportingInput(StrictModel):
    incident: IncidentReport
    scope: InvestigationScope | None
    final_reasoning: ReasoningResult | None
    final_critique: CriticResult | None
    observability: ObservabilityResult | None
    dependency: DependencyResult | None
    evidence_inventory: EvidenceInventory
    known_limitations: list[str]
    adjudication: Adjudication
    conclusion_allowed: bool  # = adjudication.decision == "cause_accepted"
    timeline_checks: list[TemporalAssessment] = Field(default_factory=list)


class ReportOutput(StrictModel):
    conclusion: Literal["probable_cause_identified", "inconclusive"]
    headline: str
    summary: str
    ranked_hypotheses: list[RankedHypothesis]
    facts: list[Statement]  # directly observed in cited evidence; no causal claims
    interpretations: list[Statement]  # inferences and causal statements, with the evidence they rest on
    assumptions: list[str]  # unverified premises the conclusion depends on
    would_confirm: list[str]  # evidence that would confirm the leading hypothesis
    would_disconfirm: list[str]  # evidence that would rule it out
    limitations: list[str]
    corrective_actions: list[CorrectiveAction]
    open_questions: list[str]


class FinalReport(StrictModel):
    incident_id: str
    investigation_id: str
    generated_by: GeneratedBy
    report: ReportOutput
    adjustments: list[str]
    critic_rounds: int
    final_critic_verdict: str | None
    adjudication: Adjudication | None = None
    evidence_inventory: EvidenceInventory | None = None
    temporal_assessments: list[TemporalAssessment] = Field(default_factory=list)
