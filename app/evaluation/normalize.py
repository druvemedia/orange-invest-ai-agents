"""Turn either approach's result into one comparable RunRecord.

Both approaches emit the same ReportOutput contract. Normalisation adds an evidence map, which says
for every reference what it points to, so grounding is scored identically for both. It also counts, the same way
for both, supporting citations that code cannot verify (``app.agents.grounding.assess_citations``), evidence gaps
and timeouts.
"""

import json
from typing import Literal

from app.agents.base import read_result
from app.agents.contracts import DependencyResult, ObservabilityResult
from app.agents.evidence import EvidenceSet, RefIndex, evidence_sets
from app.agents.grounding import assess_citations
from app.agents.reasoning import register_findings
from app.agents.orchestrator import InvestigationResult
from app.baseline import BaselineResult
from app.orchestration import EVIDENCE_TASK_KIND, TaskErrorCode
from app.orchestration.models import EvidenceOutcome
from app.schemas.common import StrictModel

Approach = Literal["multi_agent", "single_agent"]
RunStatus = Literal["completed", "fallback", "failed", "not_run"]
UNUSABLE = {EvidenceOutcome.FAILED.value, EvidenceOutcome.NOT_COLLECTED.value}
GAP_OUTCOMES = {EvidenceOutcome.FAILED, EvidenceOutcome.NOT_COLLECTED, EvidenceOutcome.PARTIAL}
TIMEOUT_CODES = {TaskErrorCode.TIMEOUT, TaskErrorCode.MODEL_TIMEOUT, TaskErrorCode.DEADLINE_EXCEEDED}


class RefInfo(StrictModel):
    kind: Literal["record", "result", "finding"]
    outcome: str | None  # outcome of the evidence result it belongs to
    text: str  # record content (records), statement (findings), tool name (results)
    resolves_to: list[str]  # findings: the evidence refs the finding cites


class NormalizedHypothesis(StrictModel):
    rank: int
    hypothesis_id: str
    title: str
    assessment: str
    confidence: str
    supporting_refs: list[str]
    contradicting_refs: list[str]


class RunRecord(StrictModel):
    case_id: str
    approach: Approach
    mode: str
    status: RunStatus
    error: str | None
    conclusion: str | None
    report_generated_by: str | None
    hypotheses: list[NormalizedHypothesis]
    fact_refs: list[str]
    evidence: dict[str, RefInfo]
    tool_calls: int
    model_calls: int
    invalid_outputs: int
    latency_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    assessed_supporting_refs: int = 0  # supporting citations of the final report's hypotheses
    unsupported_supporting_refs: int = 0  # ...that code cannot verify (unknown, missing, no-change or off-service)
    evidence_gaps: int = 0  # evidence results that failed, were never collected or are partial
    timeouts: int = 0  # timed-out tool or model attempts, and deadline stops
    termination_reason: str | None = None
    estimated_cost_usd: float | None = None  # None: no price for the model, or usage not reported (offline)


def _evidence_map(sets: list[EvidenceSet]) -> dict[str, RefInfo]:
    out = {}
    for s in sets:
        out[s.ref] = RefInfo(kind="result", outcome=s.outcome.value, text=s.tool, resolves_to=[])
        for item in s.items:
            out[item.ref] = RefInfo(kind="record", outcome=s.outcome.value,
                                    text=json.dumps(item.record, sort_keys=True), resolves_to=[])
    return out


def _hypotheses(report) -> list[NormalizedHypothesis]:
    return [NormalizedHypothesis(rank=h.rank, hypothesis_id=h.hypothesis_id, title=h.title, assessment=h.assessment,
                                 confidence=h.confidence, supporting_refs=h.supporting_refs,
                                 contradicting_refs=h.contradicting_refs)
            for h in sorted(report.ranked_hypotheses, key=lambda h: h.rank)]


def _support_counts(report, refs: RefIndex, affected_services) -> tuple[int, int]:
    rows = [row for h in report.ranked_hypotheses
            for row in assess_citations(h.hypothesis_id, f"{h.title} {h.assessment}", h.supporting_refs,
                                        h.contradicting_refs, refs, affected_services) if row.role == "supporting"]
    return len(rows), sum(1 for row in rows if row.unsupported)


def from_multi_agent(case_id, mode, result: InvestigationResult, *, latency, model_calls, tokens,
                     affected_services=()) -> RunRecord:
    tasks = result.investigation.tasks
    sets = evidence_sets(tasks)
    evidence = _evidence_map(sets)
    obs = read_result(tasks.get("observability"), ObservabilityResult)
    dep = read_result(tasks.get("dependency"), DependencyResult)
    for o in obs.output.observations if obs else []:
        evidence[f"observability/{o.observation_id}"] = RefInfo(kind="finding", outcome=None, text=o.statement, resolves_to=o.evidence_refs)
    for f in dep.output.findings if dep else []:
        evidence[f"dependency/{f.finding_id}"] = RefInfo(kind="finding", outcome=None, text=f.statement, resolves_to=f.evidence_refs)
    report = result.report
    refs = RefIndex().add_sets(sets)
    register_findings(refs, obs, dep)
    assessed, unsupported = _support_counts(report.report, refs, affected_services)
    return RunRecord(
        case_id=case_id, approach="multi_agent", mode=mode,
        status="completed" if report.generated_by == "model" else "fallback", error=None,
        conclusion=report.report.conclusion, report_generated_by=report.generated_by,
        hypotheses=_hypotheses(report.report),
        fact_refs=[r for f in report.report.facts + report.report.interpretations for r in f.evidence_refs],
        evidence=evidence,
        tool_calls=sum(len(t.attempts) for t in tasks.values() if t.spec.kind == EVIDENCE_TASK_KIND),
        model_calls=model_calls,
        invalid_outputs=sum(1 for t in tasks.values() for a in t.attempts
                            if a.error and a.error.code is TaskErrorCode.MODEL_INVALID_OUTPUT),
        latency_seconds=latency, input_tokens=tokens[0], output_tokens=tokens[1],
        assessed_supporting_refs=assessed, unsupported_supporting_refs=unsupported,
        evidence_gaps=sum(1 for s in sets if s.outcome in GAP_OUTCOMES),
        timeouts=sum(1 for t in tasks.values() for a in t.attempts if a.error and a.error.code in TIMEOUT_CODES),
        termination_reason=str(result.investigation.termination_reason) if result.investigation.termination_reason else None,
    )


def from_single_agent(case_id, mode, result: BaselineResult, *, latency, model_calls, tokens,
                      affected_services=()) -> RunRecord:
    report = result.report
    assessed, unsupported = _support_counts(report.report, RefIndex().add_sets(result.evidence), affected_services)
    return RunRecord(
        case_id=case_id, approach="single_agent", mode=mode,
        status="completed" if result.status == "completed" else "fallback", error=None,
        conclusion=report.report.conclusion, report_generated_by=report.generated_by,
        hypotheses=_hypotheses(report.report),
        fact_refs=[r for f in report.report.facts + report.report.interpretations for r in f.evidence_refs],
        evidence=_evidence_map(result.evidence),
        tool_calls=result.tool_executions,
        model_calls=model_calls,
        invalid_outputs=result.invalid_outputs,
        latency_seconds=latency, input_tokens=tokens[0], output_tokens=tokens[1],
        assessed_supporting_refs=assessed, unsupported_supporting_refs=unsupported,
        evidence_gaps=sum(1 for s in result.evidence if s.outcome in GAP_OUTCOMES),
        timeouts=(sum(1 for s in result.evidence if s.error_code == "timeout")
                  + sum(1 for s in result.steps if s.error and "timeout" in s.error.lower())),
        termination_reason=result.status,
    )


def not_run(case_id, approach: Approach, mode, reason: str) -> RunRecord:
    """A planned run that was deliberately not started (e.g. the budget stop). Scored and reported, never dropped."""
    return RunRecord(case_id=case_id, approach=approach, mode=mode, status="not_run", error=reason, conclusion=None,
                     report_generated_by=None, hypotheses=[], fact_refs=[], evidence={}, tool_calls=0, model_calls=0,
                     invalid_outputs=0, latency_seconds=0.0, input_tokens=None, output_tokens=None)


def failed_run(case_id, approach: Approach, mode, error: str, *, latency, model_calls, tokens) -> RunRecord:
    return RunRecord(case_id=case_id, approach=approach, mode=mode, status="failed", error=error, conclusion=None,
                     report_generated_by=None, hypotheses=[], fact_refs=[], evidence={}, tool_calls=0,
                     model_calls=model_calls, invalid_outputs=0, latency_seconds=latency,
                     input_tokens=tokens[0], output_tokens=tokens[1])
