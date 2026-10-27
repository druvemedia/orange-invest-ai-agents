"""Turn either approach's result into one comparable RunRecord.

Both approaches emit the same ReportOutput contract. Normalisation adds an evidence map, which says
for every reference what it points to, so grounding is scored identically for both.
"""

import json
from typing import Literal

from app.agents.base import read_result
from app.agents.contracts import DependencyResult, ObservabilityResult
from app.agents.evidence import EvidenceSet, evidence_sets
from app.agents.orchestrator import InvestigationResult
from app.baseline import BaselineResult
from app.orchestration import EVIDENCE_TASK_KIND, TaskErrorCode
from app.orchestration.models import EvidenceOutcome
from app.schemas.common import StrictModel

Approach = Literal["multi_agent", "single_agent"]
RunStatus = Literal["completed", "fallback", "failed"]
UNUSABLE = {EvidenceOutcome.FAILED.value, EvidenceOutcome.NOT_COLLECTED.value}


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


def from_multi_agent(case_id, mode, result: InvestigationResult, *, latency, model_calls, tokens) -> RunRecord:
    tasks = result.investigation.tasks
    evidence = _evidence_map(evidence_sets(tasks))
    obs = read_result(tasks.get("observability"), ObservabilityResult)
    dep = read_result(tasks.get("dependency"), DependencyResult)
    for o in obs.output.observations if obs else []:
        evidence[f"observability/{o.observation_id}"] = RefInfo(kind="finding", outcome=None, text=o.statement, resolves_to=o.evidence_refs)
    for f in dep.output.findings if dep else []:
        evidence[f"dependency/{f.finding_id}"] = RefInfo(kind="finding", outcome=None, text=f.statement, resolves_to=f.evidence_refs)
    report = result.report
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
    )


def from_single_agent(case_id, mode, result: BaselineResult, *, latency, model_calls, tokens) -> RunRecord:
    report = result.report
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
    )


def failed_run(case_id, approach: Approach, mode, error: str, *, latency, model_calls, tokens) -> RunRecord:
    return RunRecord(case_id=case_id, approach=approach, mode=mode, status="failed", error=error, conclusion=None,
                     report_generated_by=None, hypotheses=[], fact_refs=[], evidence={}, tool_calls=0,
                     model_calls=model_calls, invalid_outputs=0, latency_seconds=latency,
                     input_tokens=tokens[0], output_tokens=tokens[1])
