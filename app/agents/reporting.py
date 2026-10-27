"""Reporting agent: the final incident report, built from the whole investigation state.

Runs as the engine's finalizer, after every other task (including all critic rounds) has
finished. Code builds the evidence inventory and adjudicates whether a cause may be named
(app.agents.adjudication); the model writes the report; app.agents.report_rules then enforces
the decision, citation checks, fact/interpretation separation and evidence-availability truth.
"""

from collections.abc import Mapping

from app.agents.adjudication import adjudicate
from app.agents.base import LLMAgent, Prepared, read_envelope, read_result, read_scope, unknown_ref_problems
from app.agents.contracts import (
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
from app.agents.inventory import build_inventory
from app.agents.reasoning import rank, register_findings
from app.agents.report_rules import finalize_report
from app.agents.temporal import assess
from app.orchestration import TaskContext, TaskRecord, TaskStatus


class ReportingAgent(LLMAgent[ReportingInput, ReportOutput, FinalReport]):
    name = "reporting"
    kind = "agent.reporting"
    output_model = ReportOutput
    instructions = """
You are the Reporting agent. Write the final incident report from the investigation state you are
given: final hypotheses (already confidence-checked), the critic's reviews, findings, the evidence
inventory, known limitations and the adjudication.

- conclusion: the adjudication has already decided whether a cause may be named (conclusion_allowed).
  Write "probable_cause_identified" if it is true, otherwise "inconclusive", and explain the decision
  using the adjudication's checks and blocking issues.
- ranked_hypotheses: rank the final hypotheses (1 = most likely). Never raise a confidence level.
  The assessment covers supporting and contradicting evidence and what is still unknown.
- facts: only what cited evidence directly shows (values, log lines, changes). No causal words
  ("caused", "due to", "indicating", "likely").
- interpretations: inferences and causal statements, each citing the evidence it rests on.
- assumptions: unverified premises the conclusion depends on. Leave empty only if there are none.
- would_confirm / would_disconfirm: evidence that would confirm or rule out the leading hypothesis.
- timeline_checks: a change listed as following the first symptom cannot be presented as the cause of the
  onset; say it may have worsened the incident only if the check says so.
- limitations: say a source or signal was unavailable ONLY if evidence_inventory lists it under
  unavailable. Everything in retrieved_signals WAS retrieved.
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
        for label, statements in (("fact", output.facts), ("interpretation", output.interpretations)):
            for i, s in enumerate(statements, start=1):
                problems += unknown_ref_problems(f"{label} {i}", s.evidence_refs, prepared.refs)
        return problems

    def finish(self, ctx, output, prepared):
        inp = prepared.input
        report, adjustments = finalize_report(
            output, hypotheses=_final_hypotheses(inp), refs=prepared.refs, inventory=inp.evidence_inventory,
            adjudication=inp.adjudication, known_limitations=inp.known_limitations,
            # Deterministic (rules/fallback) reports never name a cause: there is no reviewed write-up.
            allow_accept=prepared.extra.get("generated_by") == "model", timelines=inp.timeline_checks,
        )
        return _final(ctx.investigation_id, inp, report, "model"), adjustments, []


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

    sets = evidence_sets(tasks)
    refs = RefIndex().add_sets(sets)
    register_findings(refs, obs, dep)
    inventory = build_inventory(sets)
    timelines = [assess(h, refs, incident.affected_services)
                 for h in (final_reasoning.output.hypotheses if final_reasoning else [])]
    adjudication = adjudicate(
        final_reasoning.output.hypotheses if final_reasoning else [],
        final_reasoning.output.insufficient_evidence if final_reasoning else True,
        refs,
        inventory,
        final_critique=final_critique,
        critiques=[critic_rounds[k] for k in sorted(critic_rounds)],
        critic_expected=True,
        timelines=timelines,
    )
    report_input = ReportingInput(
        incident=incident,
        scope=read_scope(tasks),
        final_reasoning=final_reasoning,
        final_critique=final_critique,
        observability=obs,
        dependency=dep,
        evidence_inventory=inventory,
        known_limitations=known_limitations(tasks, obs, dep, final_reasoning, critic_rounds),
        adjudication=adjudication,
        conclusion_allowed=adjudication.decision == "cause_accepted",
        timeline_checks=timelines,
    )
    return Prepared(input=report_input, refs=refs, consumed=sorted(tasks))


def known_limitations(tasks, obs, dep, final_reasoning, critic_rounds) -> list[str]:
    limits: list[str] = []
    for tid, t in tasks.items():
        if t.status is not TaskStatus.SUCCEEDED and not t.spec.finalizer and t.spec.kind != "evidence":
            reason = t.error.message if t.error else t.status
            limits.append(f"Task {tid} ({t.spec.kind}) did not succeed: {t.status}, {reason}")
        envelope = read_envelope(t)
        if envelope and envelope.generated_by != "model":
            limits.append(f"Task {tid} output was produced by {envelope.generated_by}, not the model.")
    if critic_rounds:
        last = critic_rounds[max(critic_rounds)]
        if last.loop_decision == "max_rounds_reached":
            limits.append(f"Critic round limit reached with verdict '{last.final_verdict}'; its remaining issues were "
                          "adjudicated on the evidence (see the adjudication).")
        if last.loop_decision == "revision_requested" and (final_reasoning is None or final_reasoning.round <= last.round):
            limits.append("The critic requested another round, but it did not run (task budget, attempt budget or failure).")
    return list(dict.fromkeys(limits))


def deterministic_report(inp: ReportingInput, why: str) -> ReportOutput:
    """An honest, model-free report: always inconclusive, with whatever the investigation established."""
    hypotheses = rank(_final_hypotheses(inp))
    findings = (inp.observability.output.observations if inp.observability else []) + (
        inp.dependency.output.findings if inp.dependency else []
    )
    return ReportOutput(
        conclusion="inconclusive",
        headline=f"Inconclusive: {why}.",
        summary=(
            f"The investigation of {inp.incident.incident_id} has no reviewed write-up because {why}. "
            "Hypotheses below are carried over unchanged from the Reasoning agent, if any; the adjudication record "
            "shows whether the evidence would have been sufficient."
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
        interpretations=[Statement(text=f.statement, evidence_refs=f.evidence_refs) for f in findings if f.kind == "interpretation"],
        assumptions=[],
        would_confirm=[q for h in hypotheses[:1] for q in h.missing_information],
        would_disconfirm=[],
        limitations=[],
        corrective_actions=[],
        open_questions=[q for h in hypotheses for q in h.missing_information],
    )


def build_fallback_report(incident, investigation_id: str, tasks: Mapping[str, TaskRecord], why: str) -> FinalReport:
    """Used by the orchestrator when the reporting task itself could not run (e.g. deadline)."""
    prepared = prepare_report_input(incident, tasks)
    inp = prepared.input
    report, notes = finalize_report(
        deterministic_report(inp, why), hypotheses=_final_hypotheses(inp), refs=prepared.refs,
        inventory=inp.evidence_inventory, adjudication=inp.adjudication, known_limitations=inp.known_limitations,
        allow_accept=False, timelines=inp.timeline_checks,
    )
    return _final(investigation_id, inp, report, "fallback").model_copy(update={"adjustments": notes})


def _final(investigation_id: str, inp: ReportingInput, report: ReportOutput, generated_by) -> FinalReport:
    return FinalReport(
        incident_id=inp.incident.incident_id,
        investigation_id=investigation_id,
        generated_by=generated_by,
        report=report,
        adjustments=[],
        critic_rounds=inp.final_critique.round if inp.final_critique else 0,
        final_critic_verdict=inp.final_critique.final_verdict if inp.final_critique else None,
        adjudication=inp.adjudication,
        evidence_inventory=inp.evidence_inventory,
        temporal_assessments=inp.timeline_checks,
    )


def _final_hypotheses(inp: ReportingInput):
    return inp.final_reasoning.output.hypotheses if inp.final_reasoning else []
