"""Reasoning agent: competing hypotheses from both evidence agents' findings, with explicit grounding."""

import re
from collections.abc import Mapping

from app.agents.base import LLMAgent, Prepared, read_result, require_scope, unknown_ref_problems
from app.agents.contracts import (
    CONFIDENCE_ORDER,
    CriticResult,
    DependencyResult,
    Hypothesis,
    InvestigationScope,
    ObservabilityResult,
    ReasoningInput,
    ReasoningOutput,
    ReasoningResult,
    TemporalAssessment,
)
from app.agents.evidence import EvidenceSet, RefIndex, duplicate_ids, evidence_sets
from app.agents.grounding import CitationCheck, check_citations
from app.agents.independence import assess_independence
from app.agents.temporal import assess
from app.orchestration import TaskContext, TaskRecord

MAX_HYPOTHESES = 5
HYPOTHESIS_ID = re.compile(r"^H\d+$")


class ReasoningAgent(LLMAgent[ReasoningInput, ReasoningOutput, ReasoningResult]):
    name = "reasoning"
    kind = "agent.reasoning"
    output_model = ReasoningOutput
    instructions = f"""
You are the Reasoning agent in an incident investigation team. You receive structured findings
from the Observability agent (telemetry) and the Dependency agent (changes and topology), the raw
evidence they cite, and, in later rounds, the Critic's issues and any newly collected evidence.

Produce up to {MAX_HYPOTHESES} competing hypotheses ("H1", "H2", ...) for what caused the incident.
When the evidence allows more than one explanation, give each one. For every hypothesis:
- supporting_refs and contradicting_refs: cite findings ("observability/obs-1") or records ("ev.logs#3").
  Look actively for contradicting evidence.
- missing_information: what evidence would confirm or rule it out.
- confidence: low, medium or high, with a rationale. High needs direct, independent evidence
  from more than one source and nothing contradicting it.
Set insufficient_evidence to true if the evidence cannot discriminate between explanations, or if
key sources are missing. In later rounds, answer each critic issue in critique_responses and use
the new evidence listed in new_evidence_refs.
""".strip()

    def prepare(self, ctx: TaskContext) -> Prepared[ReasoningInput]:
        scope, obs, dep, sets, refs = analysis_context(ctx.dependencies)
        round_no = int(ctx.task.inputs["round"])
        previous = read_result(ctx.dependencies.get(f"reasoning.r{round_no - 1}"), ReasoningResult)
        critique = read_result(ctx.dependencies.get(f"critic.r{round_no - 1}"), CriticResult)
        return Prepared(
            input=ReasoningInput(
                incident=ctx.incident,
                scope=scope,
                round=round_no,
                observability=obs,
                dependency=dep,
                evidence=sets,
                previous_hypotheses=previous.output.hypotheses if previous else [],
                critique=critique.output.issues if critique else [],
                new_evidence_refs=critique.accepted_requests if critique else [],
            ),
            refs=refs,
            consumed=sorted(ctx.dependencies),
        )

    def without_model(self, ctx, prepared):
        if any(s.items for s in prepared.input.evidence):
            return None
        return ReasoningOutput(
            hypotheses=[],
            insufficient_evidence=True,
            critique_responses=[],
            summary="No evidence records were obtained from any source, so no hypothesis can be grounded.",
        )

    def check(self, output: ReasoningOutput, prepared: Prepared[ReasoningInput]) -> list[str]:
        problems = []
        if not output.hypotheses and not output.insufficient_evidence:
            problems.append("no hypotheses, but insufficient_evidence is false")
        if len(output.hypotheses) > MAX_HYPOTHESES:
            problems.append(f"more than {MAX_HYPOTHESES} hypotheses")
        problems += [f"duplicate hypothesis id '{d}'" for d in duplicate_ids(h.hypothesis_id for h in output.hypotheses)]
        for h in output.hypotheses:
            label = h.hypothesis_id
            if not HYPOTHESIS_ID.match(label):
                problems.append(f"hypothesis id '{label}' must look like 'H1'")
            problems += unknown_ref_problems(label, h.supporting_refs + h.contradicting_refs, prepared.refs)
            problems += [
                f"{label} uses missing evidence '{r}' as support or contradiction"
                for r in prepared.refs.unusable_as_support(h.supporting_refs + h.contradicting_refs)
            ]
            both = set(h.supporting_refs) & set(h.contradicting_refs)
            problems += [f"{label} cites '{r}' as both supporting and contradicting" for r in sorted(both)]
        if prepared.input.critique and not output.critique_responses:
            problems.append("the critic raised issues but critique_responses is empty")
        return problems

    def finish(self, ctx, output, prepared):
        hypotheses, adjustments, checks, timelines = [], [], [], []
        for h in output.hypotheses:
            h, h_checks = apply_citation_checks(h, prepared.refs)
            checks += h_checks
            adjustments += [f"{c.hypothesis_id}: {c.ref} {c.action.replace('_', ' ')} ({c.detail})" for c in h_checks]
            capped, reason = cap_confidence(h, prepared.refs, output.insufficient_evidence)
            capped, timeline = apply_timeline(capped, prepared.refs, prepared.input.incident.affected_services)
            timelines.append(timeline)
            if capped.confidence != h.confidence:
                reason = reason if timeline.blocks_cause is False else f"timeline: {timeline.detail}"
                adjustments.append(f"{h.hypothesis_id}: confidence {h.confidence} -> {capped.confidence} ({reason})")
            hypotheses.append(capped)
        adjusted = output.model_copy(update={"hypotheses": hypotheses})
        return ReasoningResult(round=prepared.input.round, output=adjusted, citation_checks=checks,
                               temporal_checks=timelines), adjustments, []


def apply_timeline(h: Hypothesis, refs: RefIndex, affected_services: list[str]) -> tuple[Hypothesis, TemporalAssessment]:
    """A hypothesis whose cited change(s) cannot explain the onset is capped to low (app.agents.temporal)."""
    timeline = assess(h, refs, affected_services)
    if timeline.blocks_cause and h.confidence != "low":
        h = h.model_copy(update={"confidence": "low"})
    return h, timeline


def apply_citation_checks(h: Hypothesis, refs: RefIndex) -> tuple[Hypothesis, list[CitationCheck]]:
    supporting, contradicting, checks = check_citations(
        h.hypothesis_id, f"{h.title} {h.description}", h.supporting_refs, h.contradicting_refs, refs
    )
    if not checks:
        return h, []
    return h.model_copy(update={"supporting_refs": supporting, "contradicting_refs": contradicting}), checks


def analysis_context(
    deps: Mapping[str, TaskRecord],
) -> tuple[InvestigationScope, ObservabilityResult | None, DependencyResult | None, list[EvidenceSet], RefIndex]:
    """Everything Reasoning and Critic see: scope, both agents' findings and the cited evidence."""
    scope = require_scope(deps)
    obs = read_result(deps.get("observability"), ObservabilityResult)
    dep = read_result(deps.get("dependency"), DependencyResult)
    sets = evidence_sets(deps)
    refs = RefIndex().add_sets(sets)
    register_findings(refs, obs, dep)
    return scope, obs, dep, sets, refs


def register_findings(refs: RefIndex, obs: ObservabilityResult | None, dep: DependencyResult | None) -> None:
    for o in obs.output.observations if obs else []:
        refs.add_finding(f"observability/{o.observation_id}", o.kind, o.evidence_refs)
    for f in dep.output.findings if dep else []:
        refs.add_finding(f"dependency/{f.finding_id}", f.kind, f.evidence_refs)


def cap_confidence(h: Hypothesis, refs: RefIndex, insufficient: bool) -> tuple[Hypothesis, str | None]:
    """Code-enforced confidence ceiling. Returns the hypothesis (possibly lowered) and the reason."""
    level, reason = h.confidence, None
    direct = [r for r in h.supporting_refs if refs.is_direct_evidence(r)]
    if insufficient and level != "low":
        level, reason = "low", "evidence marked insufficient"
    elif not direct and level != "low":
        level, reason = "low", (
            "no direct, incident-specific evidence supports it; dependency edges, past incidents, interpretations "
            "and unchanged metrics are context, not evidence of failure"
        )
    elif level == "high":
        independence = assess_independence(h.supporting_refs, refs)
        if h.contradicting_refs:
            level, reason = "medium", "contradicting evidence exists"
        elif independence.status != "independent":
            level, reason = "medium", f"direct support is not independently corroborated ({independence.detail})"
    if reason is None:
        return h, None
    return h.model_copy(update={"confidence": level}), reason


def rank(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    return sorted(hypotheses, key=lambda h: -CONFIDENCE_ORDER[h.confidence])
