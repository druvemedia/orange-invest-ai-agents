"""The user-facing investigation report: the final report with every citation resolved to its evidence.

Built only from the investigation's own state. A citation that does not resolve to evidence
retrieved in this investigation is listed in ``unresolved_citations`` instead of being hidden.
The grounding checks should make that list always empty.
"""

import json
from typing import Any, Literal

from app.agents.base import read_result
from app.agents.contracts import (
    Adjudication,
    DependencyResult,
    EvidenceInventory,
    ObservabilityResult,
    TemporalAssessment,
)
from app.agents.evidence import EvidenceSet, RefIndex, evidence_sets
from app.agents.reasoning import register_findings
from app.agents.orchestrator import InvestigationResult
from app.orchestration import EVIDENCE_TASK_KIND, TaskStatus
from app.schemas.common import StrictModel
from app.schemas.incident import IncidentReport

OFFLINE_NOTICE = (
    "This report was produced by the offline heuristic stand-in (LLM_PROVIDER=offline-heuristic), not by an AI "
    "model. It applies a naive rule (most recent change, otherwise most frequent error) and is for demonstration only."
)


class Citation(StrictModel):
    ref: str
    kind: Literal["record", "result", "finding", "unresolved"]
    tool: str | None
    text: str  # one human-readable line describing what the evidence says
    outcome: str | None  # outcome of the tool result it comes from
    evidence_class: str  # direct | contextual | no_change | missing | unknown (app.agents.evidence)
    class_reason: str
    based_on: list["Citation"]  # findings: the records they cite


class HypothesisView(StrictModel):
    rank: int
    hypothesis_id: str
    title: str
    confidence: str
    assessment: str
    supporting: list[Citation]
    contradicting: list[Citation]


class StatementView(StrictModel):
    text: str
    evidence: list[Citation]


class SourceCoverage(StrictModel):
    task_id: str
    tool: str | None
    outcome: str
    record_count: int
    attempts: int
    error_code: str | None
    warnings: list[str]


class ProcessSummary(StrictModel):
    investigation_status: str
    termination_reason: str | None
    report_generated_by: str
    # Who actually wrote the report, in words a reader cannot misread (e.g. the offline stand-in is not "a model").
    report_author: str
    critic_rounds: int
    final_critic_verdict: str | None
    tasks_total: int
    tasks_not_succeeded: list[str]
    attempts_used: int
    adjustments: list[str]


class InvestigationReportView(StrictModel):
    investigation_id: str
    evidence_source: str
    model: str
    notices: list[str]
    incident: IncidentReport
    conclusion: str
    headline: str
    summary: str
    observed_impact: list[StatementView]  # observability facts, with evidence
    hypotheses: list[HypothesisView]
    facts: list[StatementView]
    interpretations: list[StatementView]
    assumptions: list[str]
    would_confirm: list[str]
    would_disconfirm: list[str]
    immediate_actions: list[str]
    follow_up_actions: list[str]
    preventive_actions: list[str]
    open_questions: list[str]
    limitations: list[str]
    evidence_coverage: list[SourceCoverage]
    evidence_inventory: EvidenceInventory | None
    adjudication: Adjudication | None
    timeline_checks: list[TemporalAssessment]
    process: ProcessSummary
    unresolved_citations: list[str]


def build_view(result: InvestigationResult, *, evidence_source: str, model: str) -> InvestigationReportView:
    inv, final = result.investigation, result.report
    report = final.report
    resolver = _Resolver(inv.tasks)
    actions: dict[str, list[str]] = {"immediate": [], "follow_up": [], "preventive": []}
    for a in report.corrective_actions:
        actions[a.timing].append(f"{a.action} (for {a.hypothesis_id})" if a.hypothesis_id else a.action)
    obs = read_result(inv.tasks.get("observability"), ObservabilityResult)
    impact = [
        StatementView(text=o.statement, evidence=resolver.many([f"observability/{o.observation_id}"])[0].based_on)
        for o in (obs.output.observations if obs else []) if o.kind == "fact"
    ]
    view = InvestigationReportView(
        investigation_id=inv.investigation_id,
        evidence_source=evidence_source,
        model=model,
        notices=[OFFLINE_NOTICE] if model == "offline-heuristic" else [],
        incident=inv.incident,
        conclusion=report.conclusion,
        headline=report.headline,
        summary=report.summary,
        observed_impact=impact,
        hypotheses=[
            HypothesisView(rank=h.rank, hypothesis_id=h.hypothesis_id, title=h.title, confidence=h.confidence,
                           assessment=h.assessment, supporting=resolver.many(h.supporting_refs),
                           contradicting=resolver.many(h.contradicting_refs))
            for h in sorted(report.ranked_hypotheses, key=lambda h: h.rank)
        ],
        facts=[StatementView(text=f.text, evidence=resolver.many(f.evidence_refs)) for f in report.facts],
        interpretations=[StatementView(text=f.text, evidence=resolver.many(f.evidence_refs)) for f in report.interpretations],
        assumptions=report.assumptions,
        would_confirm=report.would_confirm,
        would_disconfirm=report.would_disconfirm,
        immediate_actions=actions["immediate"],
        follow_up_actions=actions["follow_up"],
        preventive_actions=actions["preventive"],
        open_questions=report.open_questions,
        limitations=report.limitations,
        evidence_coverage=[
            SourceCoverage(task_id=e.task_id, tool=e.tool, outcome=e.outcome, record_count=e.record_count,
                           attempts=e.attempts, error_code=e.error_code,
                           warnings=[w for w in resolver.warnings(e.task_id) if "Absence of records" not in w])
            for e in inv.evidence
        ],
        evidence_inventory=final.evidence_inventory,
        adjudication=final.adjudication,
        timeline_checks=final.temporal_assessments,
        process=ProcessSummary(
            investigation_status=inv.status,
            termination_reason=inv.termination_reason,
            report_generated_by=final.generated_by,
            report_author=report_author(final.generated_by, model),
            critic_rounds=final.critic_rounds,
            final_critic_verdict=final.final_critic_verdict,
            tasks_total=len(inv.tasks),
            tasks_not_succeeded=[f"{tid}: {t.status}" for tid, t in inv.tasks.items() if t.status is not TaskStatus.SUCCEEDED],
            attempts_used=inv.attempts_used,
            adjustments=final.adjustments,
        ),
        unresolved_citations=sorted(resolver.unresolved),
    )
    return view


def report_author(generated_by: str, model: str) -> str:
    if generated_by == "fallback":
        return "deterministic fallback (no model output was usable)"
    if generated_by == "rules":
        return "rules (no evidence for a model to analyse)"
    if model == "offline-heuristic":
        return "offline-heuristic stand-in (not an AI model)"
    return f"model {model}"


class _Resolver:
    def __init__(self, tasks) -> None:
        self._sets: dict[str, EvidenceSet] = {s.ref: s for s in evidence_sets(tasks)}
        self._records: dict[str, tuple[EvidenceSet, dict[str, Any]]] = {
            i.ref: (s, i.record) for s in self._sets.values() for i in s.items
        }
        self._findings: dict[str, tuple[str, list[str]]] = {}
        obs = read_result(tasks.get("observability"), ObservabilityResult)
        dep = read_result(tasks.get("dependency"), DependencyResult)
        for o in obs.output.observations if obs else []:
            self._findings[f"observability/{o.observation_id}"] = (f"[{o.kind}] {o.statement}", o.evidence_refs)
        for f in dep.output.findings if dep else []:
            self._findings[f"dependency/{f.finding_id}"] = (f"[{f.kind}] {f.statement}", f.evidence_refs)
        self._index = RefIndex().add_sets(self._sets.values())
        register_findings(self._index, obs, dep)
        self.unresolved: set[str] = set()
        self._evidence_task_ids = {tid for tid, t in tasks.items() if t.spec.kind == EVIDENCE_TASK_KIND}

    def many(self, refs: list[str]) -> list[Citation]:
        return [self.one(r) for r in refs]

    def one(self, ref: str, depth: int = 0) -> Citation:
        c = self._index.classify(ref)
        cls = dict(evidence_class=c.evidence_class.value, class_reason=c.reason)
        if ref in self._records:
            s, record = self._records[ref]
            return Citation(ref=ref, kind="record", tool=s.tool, text=describe(record), outcome=s.outcome, based_on=[], **cls)
        if ref in self._sets:
            s = self._sets[ref]
            return Citation(ref=ref, kind="result", tool=s.tool, outcome=s.outcome, based_on=[], **cls,
                            text=f"{s.tool}: {s.outcome}, {len(s.items)} record(s)" + (f"; {s.error}" if s.error else ""))
        if ref in self._findings and depth < 2:
            text, inner = self._findings[ref]
            return Citation(ref=ref, kind="finding", tool=None, text=text, outcome=None, **cls,
                            based_on=[self.one(r, depth + 1) for r in inner])
        self.unresolved.add(ref)
        return Citation(ref=ref, kind="unresolved", tool=None, outcome=None, based_on=[],
                        evidence_class="unknown", class_reason="not among the evidence retrieved",
                        text="Not found in the evidence retrieved by this investigation.")

    def warnings(self, task_id: str) -> list[str]:
        s = self._sets.get(task_id)
        return s.warnings if s else []


def describe(record: dict[str, Any]) -> str:
    """One readable line per evidence record, by record type."""
    r = record
    if "message" in r and "level" in r:
        return f"{r['timestamp']} {r['service']} {r['level']}: {r['message']}"
    if "metric_name" in r:
        return f"{r['timestamp']} {r['service']} {r['metric_name']} = {r['value']:g} {r['unit']}"
    if "change_id" in r:
        details = f" {json.dumps(r['details'], sort_keys=True)}" if r.get("details") else ""
        return f"{r['timestamp']} {r['change_id']} {r['service']} {r['change_type']} ({r['status']}): {r['summary']}{details}"
    if "depends_on" in r:
        return f"{r['service']} depends on {r['depends_on']} ({r['dependency_type']}{', critical' if r['critical'] else ''})"
    if "incident_id" in r and "title" in r:
        cause = f" Recorded root cause: {r['root_cause']}." if r.get("root_cause") else " Root cause not recorded."
        return f"Past incident {r['incident_id']} ({r['occurred_at'][:10]}): {r['title']}.{cause}"
    return json.dumps(r, sort_keys=True)[:300]


def render_markdown(view: InvestigationReportView) -> str:
    v, inc = view, view.incident
    out = [f"# Investigation {v.investigation_id}: {inc.incident_id}", ""]
    for notice in v.notices:
        out += [f"> **Notice:** {notice}", ""]
    out += [
        f"**Conclusion:** {v.conclusion.replace('_', ' ')}  ",
        f"**Headline:** {v.headline}  ",
        f"**Evidence source:** {v.evidence_source} | **Model:** {v.model} | "
        f"**Status:** {v.process.investigation_status} ({v.process.termination_reason})",
        "", v.summary, "",
        "## Incident and impact", "",
        f"- Reported: {inc.reported_at.isoformat() if inc.reported_at else 'unknown'} by {inc.reporter or 'unknown'}",
        f"- Summary: {inc.summary}",
        f"- Severity: {inc.severity or 'not assessed'}; affected services: {', '.join(inc.affected_services) or 'not specified'}",
    ]
    if inc.context:
        out.append(f"- Reporter context: {inc.context}")
    if v.observed_impact:
        out += ["", "Observed in telemetry:"]
        for s in v.observed_impact:
            out.append(f"- {s.text} [{', '.join(c.ref for c in s.evidence)}]")

    out += ["", "## Ranked hypotheses", ""]
    if not v.hypotheses:
        out.append("_No hypothesis could be grounded in the evidence._")
    for h in v.hypotheses:
        out += [f"### {h.rank}. {h.title} ({h.confidence} confidence)", "", h.assessment, ""]
        out += _citations("Supporting evidence", h.supporting) + _citations("Contradicting evidence", h.contradicting)

    out += ["## Facts (directly shown by evidence)", ""]
    out += [f"- {f.text} [{', '.join(c.ref for c in f.evidence)}]" for f in v.facts] or ["_None stated._"]
    out += ["", "## Interpretations (inferences, with the evidence they rest on)", ""]
    out += [f"- {f.text} [{', '.join(c.ref for c in f.evidence)}]" for f in v.interpretations] or ["_None stated._"]
    out += ["", "## Assumptions (unverified premises)", ""]
    out += [f"- {a}" for a in v.assumptions] or ["_None: no causal hypothesis was proposed, so no premise is assumed._"
                                                if not v.hypotheses else "_None listed._"]
    out += ["", "## What would confirm or rule out the leading hypothesis", ""]
    out += [f"- Confirm: {x}" for x in v.would_confirm] + [f"- Rule out: {x}" for x in v.would_disconfirm] or ["_None stated._"]
    if v.adjudication:
        a = v.adjudication
        out += ["", "## Decision (adjudication)", "",
                f"**{a.decision.replace('_', ' ')}**"
                + (f" for {a.leading_hypothesis}" if a.leading_hypothesis and a.decision == "cause_accepted" else "")
                + f". Basis: {a.basis}.", ""]
        out += [f"- {'PASS' if c.passed else 'FAIL'} `{c.name}`: {c.detail}" for c in a.checks]
        if a.unresolved_critic_issues:
            out += ["", "Unresolved critic issues (none cited direct evidence against the leader):"]
            out += [f"- {i}" for i in a.unresolved_critic_issues]
    out += ["", "## Recommended actions", ""]
    for label, items in [("Immediate mitigation", v.immediate_actions), ("Follow-up", v.follow_up_actions),
                         ("Preventive", v.preventive_actions)]:
        out += [f"**{label}:**"] + ([f"- {a}" for a in items] or ["- _none proposed_"]) + [""]
    if v.open_questions:
        out += ["## Open questions", ""] + [f"- {q}" for q in v.open_questions] + [""]

    checked = [t for t in v.timeline_checks if t.status != "not_applicable"]
    if checked:
        out += ["", "## Timeline checks", ""]
        for t in checked:
            first = f"first symptom {t.onset} ({', '.join(t.onset_refs)})" if t.onset else "no timestamped symptom"
            out.append(f"- {t.hypothesis_id}: **{t.status.replace('_', ' ')}**; {first}")
            out += [f"  - {c.detail}" for c in t.changes]
            if t.may_have_worsened:
                out.append(f"  - symptoms rose further after the change: {', '.join(t.worsening_refs)}")
        out.append("")
    if v.evidence_inventory:
        inv = v.evidence_inventory
        out += ["## Evidence availability", "", f"Retrieved: {', '.join(inv.retrieved_signals) or 'nothing'}", ""]
        if inv.unavailable:
            out += ["Not obtained:"] + [f"- {u}" for u in inv.unavailable] + [""]
    out += ["## Missing evidence and limitations", ""]
    gaps = [c for c in v.evidence_coverage if c.outcome != "collected"]
    for c in gaps:
        out.append(f"- `{c.task_id}` ({c.tool}): **{c.outcome}**" + (f", {c.error_code}" if c.error_code else "")
                   + "".join(f"; {w}" for w in c.warnings))
    out += [f"- {lim}" for lim in v.limitations]
    if not gaps and not v.limitations:
        out.append("_None recorded._")
    if v.unresolved_citations:
        out += ["", f"**Unresolved citations (not backed by retrieved evidence):** {', '.join(v.unresolved_citations)}"]

    p = v.process
    out += ["", "## Investigation process", "",
            f"- Tasks: {p.tasks_total}; attempts used: {p.attempts_used}; critic rounds: {p.critic_rounds} "
            f"(final verdict: {p.final_critic_verdict or 'n/a'}); report written by: {p.report_author}"]
    if p.tasks_not_succeeded:
        out.append(f"- Did not succeed: {', '.join(p.tasks_not_succeeded)}")
    out += [f"- Code adjustment: {a}" for a in p.adjustments]
    out.append("")
    return "\n".join(out)


def _citations(label: str, citations: list[Citation]) -> list[str]:
    if not citations:
        return [f"_{label}: none cited._", ""]
    lines = [f"{label}:"]
    for c in citations:
        lines.append(f"- `{c.ref}` ({c.evidence_class}) {c.text}")
        lines += [f"  - `{b.ref}` ({b.evidence_class}: {b.class_reason}) {b.text}" for b in c.based_on]
    return lines + [""]
