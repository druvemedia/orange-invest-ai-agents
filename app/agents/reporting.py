"""Reporting agent: the final incident report, built from the whole investigation state.

Runs as the engine's finalizer, after every other task (including all critic rounds) has
finished. Code decides whether a probable cause may be named at all (``conclusion_allowed``)
and enforces it; the model writes the report within those limits.
"""

from collections.abc import Mapping

from app.agents.base import LLMAgent, Prepared, read_envelope, read_result, read_scope, unknown_ref_problems
from app.agents.contracts import (
    CONFIDENCE_ORDER,
    CriticResult,
    DependencyResult,
    FinalReport,
    ObservabilityResult,
    RankedHypothesis,
    ReasoningResult,
    ReportingInput,
    ReportOutput,
    Statement,
)
from app.agents.evidence import RefIndex, evidence_sets
from app.agents.reasoning import rank, register_findings
from app.orchestration import EVIDENCE_TASK_KIND, TaskContext, TaskRecord, TaskStatus
from app.orchestration.models import evidence_ref


class ReportingAgent(LLMAgent[ReportingInput, ReportOutput, FinalReport]):
    name = "reporting"
    kind = "agent.reporting"
    output_model = ReportOutput
    instructions = """
You are the Reporting agent. Write the final incident report from the investigation state you are
given: final hypotheses (already confidence-checked), the critic's last review, findings, the
evidence overview and known limitations.

- conclusion: "probable_cause_identified" only if conclusion_allowed is true AND the top hypothesis
  is clearly better supported than the rest; otherwise "inconclusive". Inconclusive is a valid result.
- ranked_hypotheses: rank the final hypotheses (1 = most likely). Never raise a confidence level.
  The assessment covers supporting and contradicting evidence and what is still unknown.
- facts: only what the cited evidence directly shows, each with references. Everything inferred
  goes in assumptions.
- limitations: include every known limitation you were given, plus any you notice.
- corrective_actions: immediate mitigation, follow-up checks to resolve uncertainty, and
  preventive measures. Tie each one to a hypothesis where relevant.
""".strip()

    def prepare(self, ctx: TaskContext) -> Prepared[ReportingInput]:
        return prepare_report_input(ctx.incident, ctx.all_tasks)

    def without_model(self, ctx, prepared):
        if prepared.input.final_reasoning is None:
            return deterministic_report(prepared.input, "no hypotheses were produced")
        return None

    def fallback(self, ctx, prepared, error):
        return deterministic_report(prepared.input, "the reporting model was unavailable")

    def check(self, output: ReportOutput, prepared: Prepared[ReportingInput]) -> list[str]:
        problems = []
        known = {h.hypothesis_id for h in _final_hypotheses(prepared.input)}
        ranks = [h.rank for h in output.ranked_hypotheses]
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            problems.append("ranks must be 1..n without gaps or repeats")
        for h in output.ranked_hypotheses:
            if h.hypothesis_id not in known:
                problems.append(f"ranked hypothesis '{h.hypothesis_id}' is not one of the final hypotheses")
            problems += unknown_ref_problems(h.hypothesis_id, h.supporting_refs + h.contradicting_refs, prepared.refs)
        for i, fact in enumerate(output.facts, start=1):
            problems += unknown_ref_problems(f"fact {i}", fact.evidence_refs, prepared.refs)
        return problems

    def finish(self, ctx, output, prepared):
        report, adjustments = enforce_report_rules(output, prepared.input)
        return _final(ctx.investigation_id, prepared.input, report, "model"), adjustments, []


def prepare_report_input(incident, tasks: Mapping[str, TaskRecord]) -> Prepared[ReportingInput]:
    reasoning_rounds = sorted(
        (r for r in (read_result(t, ReasoningResult) for tid, t in tasks.items() if tid.startswith("reasoning.")) if r),
        key=lambda r: r.round,
    )
    final_reasoning = reasoning_rounds[-1] if reasoning_rounds else None
    critic_rounds = {
        r.round: r for r in (read_result(t, CriticResult) for tid, t in tasks.items() if tid.startswith("critic.")) if r
    }
    final_critique = critic_rounds.get(final_reasoning.round) if final_reasoning else None
    obs = read_result(tasks.get("observability"), ObservabilityResult)
    dep = read_result(tasks.get("dependency"), DependencyResult)

    refs = RefIndex().add_sets(evidence_sets(tasks))
    register_findings(refs, obs, dep)
    report_input = ReportingInput(
        incident=incident,
        scope=read_scope(tasks),
        final_reasoning=final_reasoning,
        final_critique=final_critique,
        observability=obs,
        dependency=dep,
        evidence_overview=[
            evidence_ref(t).model_dump(mode="json") for t in tasks.values() if t.spec.kind == EVIDENCE_TASK_KIND
        ],
        known_limitations=known_limitations(tasks, obs, dep, final_reasoning, critic_rounds),
        conclusion_allowed=conclusion_allowed(final_reasoning, final_critique),
    )
    return Prepared(input=report_input, refs=refs, consumed=sorted(tasks))


def conclusion_allowed(reasoning: ReasoningResult | None, critique: CriticResult | None) -> bool:
    """A probable cause may be named only when it is grounded, reviewed and clearly ahead."""
    if reasoning is None or reasoning.output.insufficient_evidence or not reasoning.output.hypotheses:
        return False
    if critique is None or critique.final_verdict != "accept":
        return False
    levels = sorted((CONFIDENCE_ORDER[h.confidence] for h in reasoning.output.hypotheses), reverse=True)
    top_is_medium_or_better = levels[0] >= CONFIDENCE_ORDER["medium"]
    clearly_ahead = len(levels) == 1 or levels[0] > levels[1]
    return top_is_medium_or_better and clearly_ahead


def known_limitations(tasks, obs, dep, final_reasoning, critic_rounds) -> list[str]:
    limits: list[str] = []
    for tid, t in tasks.items():
        if t.status is not TaskStatus.SUCCEEDED and not t.spec.finalizer:
            reason = t.error.message if t.error else t.status
            limits.append(f"Task {tid} ({t.spec.kind}) did not succeed: {t.status}, {reason}")
        envelope = read_envelope(t)
        if envelope and envelope.generated_by != "model":
            limits.append(f"Task {tid} output was produced by {envelope.generated_by}, not the model.")
    for result in (obs, dep):
        limits += result.data_gaps if result else []
    if critic_rounds:
        last = critic_rounds[max(critic_rounds)]
        if last.loop_decision == "max_rounds_reached":
            limits.append(f"Critic round limit reached with verdict '{last.final_verdict}'; issues may remain unresolved.")
        if last.loop_decision == "revision_requested" and (final_reasoning is None or final_reasoning.round <= last.round):
            limits.append("The critic requested another round, but it did not run (task budget, attempt budget or failure).")
    return list(dict.fromkeys(limits))


def enforce_report_rules(output: ReportOutput, inp: ReportingInput) -> tuple[ReportOutput, list[str]]:
    adjustments = []
    report = output
    if report.conclusion == "probable_cause_identified" and not inp.conclusion_allowed:
        report = report.model_copy(update={"conclusion": "inconclusive"})
        adjustments.append("Conclusion changed to inconclusive: the evidence and review do not allow a probable cause.")

    ceiling = {h.hypothesis_id: h.confidence for h in _final_hypotheses(inp)}
    ranked = []
    for h in report.ranked_hypotheses:
        allowed = ceiling.get(h.hypothesis_id, "low")
        if CONFIDENCE_ORDER[h.confidence] > CONFIDENCE_ORDER[allowed]:
            adjustments.append(f"{h.hypothesis_id}: report confidence {h.confidence} lowered to {allowed}.")
            h = h.model_copy(update={"confidence": allowed})
        ranked.append(h)

    facts = [f for f in report.facts if f.evidence_refs]
    unsupported = [f.text for f in report.facts if not f.evidence_refs]
    if unsupported:
        adjustments.append(f"{len(unsupported)} uncited 'fact(s)' moved to assumptions.")
    limitations = list(dict.fromkeys([*inp.known_limitations, *report.limitations]))
    report = report.model_copy(
        update={
            "ranked_hypotheses": ranked,
            "facts": facts,
            "assumptions": [*report.assumptions, *unsupported],
            "limitations": limitations,
        }
    )
    return report, adjustments


def deterministic_report(inp: ReportingInput, why: str) -> ReportOutput:
    """An honest, model-free report: inconclusive, with whatever the investigation established."""
    hypotheses = rank(_final_hypotheses(inp))
    findings = (inp.observability.output.observations if inp.observability else []) + (
        inp.dependency.output.findings if inp.dependency else []
    )
    return ReportOutput(
        conclusion="inconclusive",
        headline=f"Inconclusive: {why}.",
        summary=(
            f"The investigation of {inp.incident.incident_id} could not reach a reviewed conclusion because {why}. "
            "Hypotheses below are carried over unchanged from the Reasoning agent, if any."
        ),
        ranked_hypotheses=[
            RankedHypothesis(
                rank=i,
                hypothesis_id=h.hypothesis_id,
                title=h.title,
                confidence=h.confidence,
                supporting_refs=h.supporting_refs,
                contradicting_refs=h.contradicting_refs,
                assessment="Not reviewed by the reporting model.",
            )
            for i, h in enumerate(hypotheses, start=1)
        ],
        facts=[Statement(text=f.statement, evidence_refs=f.evidence_refs) for f in findings if f.kind == "fact"],
        assumptions=[f.statement for f in findings if f.kind == "interpretation"],
        limitations=list(inp.known_limitations),
        corrective_actions=[],
        open_questions=[q for h in hypotheses for q in h.missing_information],
    )


def build_fallback_report(incident, investigation_id: str, tasks: Mapping[str, TaskRecord], why: str) -> FinalReport:
    """Used by the orchestrator when the reporting task itself could not run (e.g. deadline)."""
    inp = prepare_report_input(incident, tasks).input
    return _final(investigation_id, inp, deterministic_report(inp, why), "fallback")


def _final(investigation_id: str, inp: ReportingInput, report: ReportOutput, generated_by) -> FinalReport:
    return FinalReport(
        incident_id=inp.incident.incident_id,
        investigation_id=investigation_id,
        generated_by=generated_by,
        report=report,
        adjustments=[],
        critic_rounds=inp.final_critique.round if inp.final_critique else 0,
        final_critic_verdict=inp.final_critique.final_verdict if inp.final_critique else None,
    )


def _final_hypotheses(inp: ReportingInput):
    return inp.final_reasoning.output.hypotheses if inp.final_reasoning else []
