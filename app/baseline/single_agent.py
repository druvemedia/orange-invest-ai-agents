"""Single-agent baseline for the evaluation.

One model runs the whole investigation in a loop. Each step it sees the incident, the tool
catalogue, its remaining budget and all evidence gathered so far, and either requests tool calls or
writes the final report. Compared with the multi-agent system it uses:

- the same five registered evidence tools, the same data and the same simulated faults;
- the same LLMClient abstraction, structured outputs and shared evidence rules (SHARED_RULES);
- the same tool-call validation: registered tool, schema-valid query, no repeated query;
- the same evidence grounding checks: only shown references, failed evidence is never support;
- the same code-enforced confidence cap, and the same report contract (ReportOutput).

Differences, inherent to having one agent: no critic review, so the multi-agent rule "a probable
cause needs the critic's acceptance" has no equivalent here. The baseline's conclusion rule is
otherwise the same (top hypothesis at least medium and strictly ahead of the next).
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from app.agents.base import SHARED_RULES, unknown_ref_problems
from app.agents.contracts import (
    TOOL_NAMES,
    Adjudication,
    EvidenceInventory,
    FinalReport,
    TemporalAssessment,
    Hypothesis,
    ReportOutput,
)
from app.agents.evidence import EvidenceItem, EvidenceSet, RefIndex, duplicate_ids, metric_query_start, parse_instant
from app.agents.adjudication import adjudicate
from app.agents.inventory import build_inventory
from app.agents.reasoning import apply_citation_checks, apply_timeline, cap_confidence
from app.agents.report_rules import finalize_report
from app.core.config import Settings
from app.core.redaction import redact
from app.llm import LLMClient, LLMError, LLMErrorKind, LLMRequest
from app.orchestration.models import EvidenceOutcome
from app.schemas.common import StrictModel
from app.schemas.evidence import EvidenceResult, EvidenceSource, EvidenceStatus
from app.schemas.incident import IncidentReport
from app.tools.base import EvidenceTool
from app.tools.datastore import summarize_validation_error

logger = logging.getLogger(__name__)

AGENT_NAME = "baseline"

_OUTCOMES = {
    EvidenceStatus.OK: EvidenceOutcome.COLLECTED,
    EvidenceStatus.NO_MATCHES: EvidenceOutcome.NO_MATCHES,
    EvidenceStatus.PARTIAL: EvidenceOutcome.PARTIAL,
    EvidenceStatus.FAILED: EvidenceOutcome.FAILED,
}


class BaselineConfig(StrictModel):
    max_steps: int = Field(default=6, ge=2, le=12)
    max_tool_calls: int = Field(default=14, ge=1, le=40)
    max_tool_calls_per_step: int = Field(default=6, ge=1, le=10)
    model_attempts_per_step: int = Field(default=2, ge=1, le=3)
    step_timeout_seconds: float = Field(default=150.0, gt=0, le=900)
    evidence_attempts: int = Field(default=2, ge=1, le=3)
    evidence_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    deadline_seconds: float = Field(default=300.0, gt=0, le=3600)

    @classmethod
    def from_settings(cls, settings: Settings) -> "BaselineConfig":
        return cls(step_timeout_seconds=settings.llm_timeout_seconds + 30, deadline_seconds=settings.investigation_deadline_seconds)


# --- Model-facing contracts ----------------------------------------------------------------------


class ToolCall(StrictModel):
    tool: str = Field(json_schema_extra={"enum": list(TOOL_NAMES)})
    query_json: str
    reason: str


class BaselineStep(StrictModel):
    action: Literal["call_tools", "final_report"]
    tool_calls: list[ToolCall]
    report: ReportOutput | None


class RejectedCall(StrictModel):
    tool: str
    query_json: str
    reason: str


class BaselineInput(StrictModel):
    incident: IncidentReport
    step: int
    max_steps: int
    must_report_now: bool
    remaining_tool_calls: int
    max_tool_calls_per_step: int
    available_tools: dict[str, dict[str, Any]]
    evidence: list[EvidenceSet]
    rejected_tool_calls: list[RejectedCall]


INSTRUCTIONS = """
You are an incident investigator working alone. Find the most likely root cause of the incident using
the registered evidence tools, then write the final report.

Each step, either:
- action "call_tools": request up to max_tool_calls_per_step tool calls. Each names a tool from
  available_tools and gives query_json, a JSON object matching that tool's schema, with a reason.
  Plan broadly first (logs, metrics, recent changes, dependencies, similar past incidents), then
  follow up on what you find. Do not repeat a query you already ran. Set report to null.
- action "final_report": set tool_calls to [] and write the report. You must do this when
  must_report_now is true.

The report:
- ranked_hypotheses: competing explanations, rank 1 most likely. Each cites supporting and contradicting
  evidence. Look actively for contradicting evidence. Confidence is low, medium or high; high needs direct,
  independent evidence from more than one source and nothing contradicting it.
- conclusion: "probable_cause_identified" only if the top hypothesis is clearly better supported than the
  rest; otherwise "inconclusive", which is a valid result. Code re-checks this against the evidence.
- facts: only what cited evidence directly shows, with no causal words. Causal statements go in
  interpretations (with citations); unverified premises go in assumptions.
- would_confirm / would_disconfirm: evidence that would confirm or rule out the leading hypothesis.
- limitations: say a source was unavailable only if your tool calls for it failed or were never made.
- corrective_actions (immediate, follow_up, preventive) and open_questions.
""".strip()


# --- Result ----------------------------------------------------------------------------------------


class StepRecord(StrictModel):
    step: int
    action: str | None
    accepted_calls: list[str]
    rejected_calls: list[RejectedCall]
    error: str | None


class BaselineResult(StrictModel):
    investigation_id: str
    incident_id: str
    status: Literal["completed", "fallback"]
    report: FinalReport
    evidence: list[EvidenceSet]
    steps: list[StepRecord]
    tool_executions: int
    model_calls: int
    invalid_outputs: int
    elapsed_seconds: float


class _State:
    def __init__(self, reported_at: datetime | None = None, affected_services: list[str] | None = None) -> None:
        self.reported_at = reported_at
        self.affected_services = affected_services
        self.sets: list[EvidenceSet] = []
        self.query_keys: set[str] = set()
        self.tool_calls_used = 0
        self.tool_executions = 0
        self.model_calls = 0
        self.invalid_outputs = 0
        self.steps: list[StepRecord] = []
        self.rejected: list[RejectedCall] = []
        self.limitations: list[str] = []


class SingleAgentInvestigator:
    def __init__(self, llm: LLMClient, tools: dict[str, EvidenceTool], config: BaselineConfig | None = None) -> None:
        self._llm = llm
        self._tools = dict(tools)
        self.config = config or BaselineConfig()

    async def investigate(self, incident: IncidentReport, investigation_id: str | None = None) -> BaselineResult:
        investigation_id = investigation_id or f"base-{uuid4().hex[:12]}"
        state = _State(incident.reported_at, incident.affected_services)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.config.deadline_seconds):
                report, why = await self._loop(incident, state)
        except TimeoutError:
            report, why = None, f"the deadline of {self.config.deadline_seconds:g}s was exceeded"

        if report is None:
            status, generated_by = "fallback", "fallback"
            report = _fallback_report(incident, why or "no report was produced")
        else:
            status, generated_by = "completed", "model"
        report, adjustments, adjudication, inventory, timelines = enforce_rules(
            report, state.sets, state.limitations, allow_accept=generated_by == "model",
            affected_services=incident.affected_services)
        final = FinalReport(
            incident_id=incident.incident_id,
            investigation_id=investigation_id,
            generated_by=generated_by,
            report=report,
            adjustments=adjustments,
            critic_rounds=0,
            final_critic_verdict=None,
            adjudication=adjudication,
            evidence_inventory=inventory,
            temporal_assessments=timelines,
        )
        return BaselineResult(
            investigation_id=investigation_id,
            incident_id=incident.incident_id,
            status=status,
            report=final,
            evidence=state.sets,
            steps=state.steps,
            tool_executions=state.tool_executions,
            model_calls=state.model_calls,
            invalid_outputs=state.invalid_outputs,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )

    # --- Loop ------------------------------------------------------------------------------------

    async def _loop(self, incident: IncidentReport, state: _State) -> tuple[ReportOutput | None, str | None]:
        cfg = self.config
        for step in range(1, cfg.max_steps + 1):
            remaining = cfg.max_tool_calls - state.tool_calls_used
            must_report = step == cfg.max_steps or remaining <= 0
            inp = BaselineInput(
                incident=incident,
                step=step,
                max_steps=cfg.max_steps,
                must_report_now=must_report,
                remaining_tool_calls=max(0, remaining),
                max_tool_calls_per_step=cfg.max_tool_calls_per_step,
                available_tools={
                    name: {"description": t.description, "query_schema": t.query_model.model_json_schema()}
                    for name, t in self._tools.items()
                },
                evidence=state.sets,
                rejected_tool_calls=state.rejected,
            )
            state.rejected = []
            try:
                out = await self._ask(inp, state)
            except LLMError as error:
                state.steps.append(StepRecord(step=step, action=None, accepted_calls=[], rejected_calls=[], error=error.kind.value))
                state.limitations.append(f"Model failed at step {step} ({error.kind}): {redact(error.message)}")
                return None, f"the model failed at step {step} ({error.kind})"

            if out.action == "final_report" or must_report:
                state.steps.append(StepRecord(step=step, action="final_report", accepted_calls=[], rejected_calls=[], error=None))
                if out.report is None:
                    state.limitations.append(f"Step {step}: a report was required but the model requested tools.")
                    return None, "the model did not produce a report within its step budget"
                if remaining <= 0 and out.action != "final_report":
                    state.limitations.append("Tool-call budget exhausted; report forced.")
                return out.report, None

            accepted, rejected = self._validate_calls(out.tool_calls, state, remaining)
            state.rejected = rejected
            ids = [f"b{step}.{i + 1}" for i in range(len(accepted))]
            state.steps.append(StepRecord(step=step, action="call_tools", accepted_calls=ids, rejected_calls=rejected, error=None))
            state.tool_calls_used += len(accepted)
            new_sets = await asyncio.gather(*(self._run_tool(ref, tool, q, state) for ref, (tool, q) in zip(ids, accepted)))
            state.sets.extend(new_sets)
        return None, "the step budget ran out"

    async def _ask(self, inp: BaselineInput, state: _State) -> BaselineStep:
        request = LLMRequest(agent=AGENT_NAME, instructions=f"{INSTRUCTIONS}\n\n{SHARED_RULES}", input_json=inp.model_dump_json())
        refs = RefIndex().add_sets(inp.evidence)
        last: LLMError | None = None
        for _ in range(self.config.model_attempts_per_step):
            state.model_calls += 1
            try:
                async with asyncio.timeout(self.config.step_timeout_seconds):
                    out = await self._llm.generate(request, BaselineStep)
                problems = check_step(out, refs, inp.must_report_now)
                if problems:
                    raise LLMError(LLMErrorKind.INVALID_OUTPUT, redact("Output failed checks: " + "; ".join(problems[:5])))
                return out
            except TimeoutError:
                last = LLMError(LLMErrorKind.TIMEOUT, "The model step exceeded its timeout.")
            except LLMError as error:
                last = error
            if last.kind is LLMErrorKind.INVALID_OUTPUT:
                state.invalid_outputs += 1
            if not last.retryable:
                break
        raise last  # type: ignore[misc]

    def _validate_calls(self, calls: list[ToolCall], state: _State, remaining: int):
        accepted: list[tuple[EvidenceTool, dict]] = []
        rejected: list[RejectedCall] = []
        limit = min(self.config.max_tool_calls_per_step, remaining)
        for call in calls:
            reason, query = None, None
            tool = self._tools.get(call.tool)
            if tool is None:
                reason = f"unknown tool '{call.tool[:64]}'"
            else:
                try:
                    raw = json.loads(call.query_json)
                    if not isinstance(raw, dict):
                        raise ValueError("query_json must be a JSON object")
                    query = tool.query_model.model_validate(raw).model_dump(mode="json")
                    if tool.source is EvidenceSource.METRICS and (start := parse_instant(query.get("start"))):
                        # Same guarantee as the multi-agent plan: metrics include a pre-incident baseline.
                        query["start"] = metric_query_start(start, state.reported_at).isoformat()
                except ValidationError as exc:
                    reason = f"invalid query: {summarize_validation_error(exc)}"
                except ValueError as exc:
                    reason = f"invalid query: {type(exc).__name__}"
            key = call.tool + json.dumps(query, sort_keys=True) if query is not None else None
            if reason is None and key in state.query_keys:
                reason = "the same query was already run"
            if reason is None and len(accepted) >= limit:
                reason = "exceeds the tool-call budget"
            if reason:
                rejected.append(RejectedCall(tool=call.tool[:64], query_json=call.query_json[:2000], reason=reason))
                continue
            state.query_keys.add(key)
            accepted.append((tool, query))
        return accepted, rejected

    async def _run_tool(self, ref: str, tool: EvidenceTool, query: dict, state: _State) -> EvidenceSet:
        result: EvidenceResult | None = None
        for _ in range(self.config.evidence_attempts):
            state.tool_executions += 1
            try:
                async with asyncio.timeout(self.config.evidence_timeout_seconds):
                    result = await asyncio.to_thread(tool.run, query)
            except TimeoutError:
                result = None
                continue
            if result.status is not EvidenceStatus.FAILED or not all(e.retryable for e in result.errors):
                break
        return evidence_set(ref, tool.name, query, result, state.reported_at, state.affected_services)


# --- Helpers -----------------------------------------------------------------------------------------


def evidence_set(ref: str, tool_name: str, query: dict, result: EvidenceResult | None,
                 reported_at: datetime | None = None, affected_services: list[str] | None = None) -> EvidenceSet:
    context = dict(incident_reported_at=reported_at.isoformat() if reported_at else None,
                   incident_affected_services=list(affected_services) if affected_services is not None else None)
    if result is None:
        return EvidenceSet(ref=ref, tool=tool_name, source=None, outcome=EvidenceOutcome.FAILED, query=query,
                           warnings=[], error="The tool call timed out.", items=[], error_code="timeout", **context)
    outcome = _OUTCOMES[result.status]
    items = [] if outcome is EvidenceOutcome.FAILED else [
        EvidenceItem(ref=f"{ref}#{i}", record=r.model_dump(mode="json")) for i, r in enumerate(result.records)
    ]
    return EvidenceSet(ref=ref, tool=tool_name, source=result.source, outcome=outcome, query=result.query,
                       warnings=list(result.warnings), error=result.errors[0].message if result.errors else None,
                       items=items, error_code=result.errors[0].code.value if result.errors else None, **context)


def check_step(out: BaselineStep, refs: RefIndex, must_report: bool) -> list[str]:
    problems = []
    if out.action == "call_tools" and not out.tool_calls and not must_report:
        problems.append("call_tools with no tool calls")
    report = out.report
    if (out.action == "final_report" or must_report) and report is None:
        problems.append("a final report is required but report is null")
    if report is None:
        return problems
    ranks = [h.rank for h in report.ranked_hypotheses]
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        problems.append("ranks must be 1..n without gaps or repeats")
    problems += [f"duplicate hypothesis id '{d}'" for d in duplicate_ids(h.hypothesis_id for h in report.ranked_hypotheses)]
    for h in report.ranked_hypotheses:
        cited = h.supporting_refs + h.contradicting_refs
        problems += unknown_ref_problems(h.hypothesis_id, cited, refs)
        problems += [f"{h.hypothesis_id} uses missing evidence '{r}' as support or contradiction"
                     for r in refs.unusable_as_support(cited)]
    for i, fact in enumerate(report.facts, start=1):
        problems += unknown_ref_problems(f"fact {i}", fact.evidence_refs, refs)
    return problems


def enforce_rules(
    report: ReportOutput, sets: list[EvidenceSet], step_limitations: list[str], *, allow_accept: bool,
    affected_services: list[str] = (),
) -> tuple[ReportOutput, list[str], Adjudication, EvidenceInventory, list[TemporalAssessment]]:
    """The same code-enforced rules the multi-agent system applies (see module docstring and report_rules)."""
    refs = RefIndex().add_sets(sets)
    inventory = build_inventory(sets)
    notes, hypotheses, timelines = [], [], []
    for h in report.ranked_hypotheses:
        as_hypothesis = Hypothesis(
            hypothesis_id=h.hypothesis_id, title=h.title, description=h.assessment,
            supporting_refs=h.supporting_refs, contradicting_refs=h.contradicting_refs,
            missing_information=[], confidence=h.confidence, confidence_rationale="",
        )
        as_hypothesis, checks = apply_citation_checks(as_hypothesis, refs)
        notes += [f"{c.hypothesis_id}: {c.ref} {c.action.replace('_', ' ')} ({c.detail})" for c in checks]
        capped, reason = cap_confidence(as_hypothesis, refs, insufficient=False)
        capped, timeline = apply_timeline(capped, refs, list(affected_services))
        timelines.append(timeline)
        if capped.confidence != h.confidence:
            reason = f"timeline: {timeline.detail}" if timeline.blocks_cause else reason
            notes.append(f"{h.hypothesis_id}: confidence {h.confidence} -> {capped.confidence} ({reason})")
        hypotheses.append(capped)
    adjudication = adjudicate(hypotheses, not hypotheses, refs, inventory,
                              final_critique=None, critiques=None, critic_expected=False, timelines=timelines)
    final, more = finalize_report(report, hypotheses=hypotheses, refs=refs, inventory=inventory,
                                  adjudication=adjudication, known_limitations=step_limitations,
                                  allow_accept=allow_accept, timelines=timelines)
    return final, notes + more, adjudication, inventory, timelines


def _fallback_report(incident: IncidentReport, why: str) -> ReportOutput:
    return ReportOutput(
        conclusion="inconclusive",
        headline=f"Inconclusive: {why}.",
        summary=f"The single-agent investigation of {incident.incident_id} did not produce a report because {why}.",
        ranked_hypotheses=[], facts=[], interpretations=[], assumptions=[], would_confirm=[], would_disconfirm=[],
        limitations=[f"No report: {why}."], corrective_actions=[], open_questions=[],
    )
