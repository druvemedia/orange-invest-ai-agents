"""Observability agent: turns log and metric evidence into cited observations."""

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from app.agents.base import LLMAgent, Prepared, require_scope, unknown_ref_problems
from app.agents.contracts import (
    MetricSummary,
    ObservabilityInput,
    ObservabilityOutput,
    ObservabilityResult,
)
from app.agents.evidence import EvidenceSet, RefIndex, duplicate_ids, evidence_sets, parse_instant
from app.orchestration import TaskContext
from app.orchestration.models import EvidenceOutcome
from app.schemas.evidence import EvidenceSource


class ObservabilityAgent(LLMAgent[ObservabilityInput, ObservabilityOutput, ObservabilityResult]):
    name = "observability"
    kind = "agent.observability"
    output_model = ObservabilityOutput
    instructions = """
You are the Observability agent in an incident investigation team. You receive log and metric
evidence, plus metric summaries computed in code. Report what the telemetry shows.

Return observations, each with a unique id "obs-1", "obs-2", ...:
- kind "fact": directly visible in the cited records (a value, a log message, a timestamp).
- kind "interpretation": your reading of facts (e.g. "latency rose after 14:00, suggesting saturation").
  Interpretations must still cite the records they rest on.
Every observation cites at least one reference. Estimate the onset time only if the evidence shows it.
List evidence gaps: missing, partial or failed sources, and services with no telemetry.
Do not name a root cause; that is the Reasoning agent's job.
""".strip()

    def prepare(self, ctx: TaskContext) -> Prepared[ObservabilityInput]:
        scope = require_scope(ctx.dependencies)
        sets = evidence_sets(ctx.dependencies)
        refs = RefIndex().add_sets(sets)
        return Prepared(
            input=ObservabilityInput(
                incident=ctx.incident, scope=scope, evidence=sets, metric_summaries=summarise_metrics(sets, refs)
            ),
            refs=refs,
            consumed=sorted(ctx.dependencies),
        )

    def without_model(self, ctx, prepared):
        if any(s.items for s in prepared.input.evidence):
            return None
        return ObservabilityOutput(
            observations=[],
            onset_estimate=None,
            evidence_gaps=data_gaps(prepared.input.evidence),
            summary="No log or metric records were obtained; nothing can be observed.",
        )

    def check(self, output: ObservabilityOutput, prepared: Prepared[ObservabilityInput]) -> list[str]:
        problems = []
        has_records = any(s.items for s in prepared.input.evidence)
        if has_records and not output.observations:
            problems.append("no observations although evidence records exist")
        ids = [o.observation_id for o in output.observations]
        problems += [f"duplicate observation id '{d}'" for d in duplicate_ids(ids)]
        for o in output.observations:
            if not o.observation_id.startswith("obs-"):
                problems.append(f"observation id '{o.observation_id}' must look like 'obs-1'")
            if not o.statement.strip():
                problems.append(f"{o.observation_id} has an empty statement")
            if not o.evidence_refs:
                problems.append(f"{o.observation_id} cites no evidence")
            problems += unknown_ref_problems(o.observation_id, o.evidence_refs, prepared.refs)
            if o.kind == "fact":
                bad = prepared.refs.unusable_as_support(o.evidence_refs)
                problems += [f"{o.observation_id} states a fact from missing evidence '{r}'" for r in bad]
        return problems

    def finish(self, ctx, output, prepared):
        result = ObservabilityResult(
            output=output,
            metric_summaries=prepared.input.metric_summaries,
            data_gaps=data_gaps(prepared.input.evidence),
        )
        return result, [], []


def summarise_metrics(sets: list[EvidenceSet], refs: RefIndex | None = None) -> list[MetricSummary]:
    """Per-series summaries for the model. The baseline is the same pre-onset baseline the classifier uses."""
    refs = refs or RefIndex().add_sets(sets)
    # Per series: sample time -> (ref, record). Keyed by normalised time, like the classifier's series, so a sample
    # retrieved twice counts once and the first retrieval's reference is kept.
    series: dict[tuple[str, str, str], dict[datetime, tuple[str, dict[str, Any]]]] = defaultdict(dict)
    for s in sets:
        if s.source is not EvidenceSource.METRICS:
            continue
        for item in s.items:
            r = item.record
            if (t := parse_instant(r.get("timestamp"))) is not None:
                series[(r["service"], r["metric_name"], r["unit"])].setdefault(t, (item.ref, r))
    summaries = []
    for (service, metric, unit), by_time in series.items():
        points = [by_time[t] for t in sorted(by_time)]
        values = [p[1]["value"] for p in points]
        head, basis = refs.metric_baseline(service, metric)
        peak_index = max(range(len(values)), key=values.__getitem__)
        summaries.append(
            MetricSummary(
                service=service,
                metric_name=metric,
                unit=unit,
                samples=len(values),
                baseline=round(median(head), 3) if head else None,
                baseline_basis=f"median of {basis}" if head else basis,
                minimum=min(values),
                peak=values[peak_index],
                peak_at=points[peak_index][1]["timestamp"],
                last=values[-1],
                first_ref=points[0][0],
                peak_ref=points[peak_index][0],
                last_ref=points[-1][0],
            )
        )
    return sorted(summaries, key=lambda m: (m.service, m.metric_name))


def data_gaps(sets: list[EvidenceSet]) -> list[str]:
    """Deterministic statements about missing evidence; never left to the model to remember."""
    gaps = []
    for s in sets:
        if s.outcome in (EvidenceOutcome.FAILED, EvidenceOutcome.NOT_COLLECTED):
            gaps.append(f"{s.ref} ({s.tool}): no evidence obtained ({s.outcome}). {s.error or ''}".strip())
        elif s.outcome is EvidenceOutcome.PARTIAL:
            gaps += [f"{s.ref} ({s.tool}) is partial: {w}" for w in s.warnings if "Absence of records" not in w]
    return gaps
