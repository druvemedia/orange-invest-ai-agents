"""Evidence references shared by every agent, and what each cited reference actually shows.

Reference formats. Each is valid only if the agent was actually shown it:

    "<task_id>"            a whole evidence result; cite it for gaps, absence or failure
    "<task_id>#<n>"        record n (0-based) of that result, e.g. "ev.logs#3"
    "<task_id>/<id>"       a finding produced by an agent task, e.g. "observability/obs-2"

Every reference resolves back to evidence tool results, so provenance is never lost.

Evidence classes (deterministic, see ``RefIndex.classify``):
    direct      an incident-specific observation: a log line, a change record, or a metric sample that
                deviates from its own series' baseline in the retrieved data by more than its noise
    contextual  true but not evidence of failure in this incident: a dependency edge (a relationship),
                a past incident, an interpretation, a whole result, or a metric that cannot be compared
    no_change   a metric sample within the normal range of its series: it cannot show an increase or drop
    missing     a result whose tool failed or never ran: it shows a gap, not evidence either way
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from app.orchestration.models import (
    EVIDENCE_TASK_KIND,
    EvidenceOutcome,
    TaskRecord,
    TaskStatus,
    evidence_ref,
)
from app.schemas.common import StrictModel
from app.schemas.evidence import EvidenceSource

UNUSABLE_AS_SUPPORT = frozenset({EvidenceOutcome.FAILED, EvidenceOutcome.NOT_COLLECTED})
# A metric sample is a change only if it differs from its series' baseline (the median of the earliest third
# of retrieved samples) by at least this fraction AND by at least NOISE_SIGMAS times the baseline noise.
CHANGE_THRESHOLD = 0.25
NOISE_SIGMAS = 3.0


class EvidenceItem(StrictModel):
    ref: str
    record: dict[str, Any]


class EvidenceSet(StrictModel):
    """Everything one evidence task produced (or failed to produce), as an agent sees it."""

    ref: str
    tool: str
    source: EvidenceSource | None
    outcome: EvidenceOutcome
    query: dict[str, Any]
    warnings: list[str]
    error: str | None
    items: list[EvidenceItem]
    error_code: str | None = None
    requested_by: str | None = None  # e.g. "critic.r1" for a follow-up query


def evidence_sets(tasks: Mapping[str, TaskRecord], task_ids: Iterable[str] | None = None) -> list[EvidenceSet]:
    """Build EvidenceSets for the evidence tasks among ``task_ids`` (default: all), in task order."""
    wanted = set(task_ids) if task_ids is not None else None
    sets = []
    for task_id, record in tasks.items():
        if record.spec.kind != EVIDENCE_TASK_KIND or (wanted is not None and task_id not in wanted):
            continue
        ref = evidence_ref(record)
        result = record.output.evidence if record.output else None
        if result is None and record.attempts:
            result = record.attempts[-1].evidence  # a failed tool result still shows the query and error
        items = (
            [EvidenceItem(ref=f"{task_id}#{i}", record=r.model_dump(mode="json")) for i, r in enumerate(result.records)]
            if record.status is TaskStatus.SUCCEEDED and result is not None
            else []
        )
        sets.append(
            EvidenceSet(
                ref=task_id,
                tool=ref.tool or "",
                source=result.source if result else None,
                outcome=ref.outcome,
                query=result.query if result else dict(record.spec.inputs.get("query", {})),
                warnings=list(result.warnings) if result else [],
                error=record.error.message if record.error else None,
                items=items,
                error_code=record.error.code.value if record.error else None,
                requested_by=record.added_by if record.added_by and record.added_by != "orchestrate" else None,
            )
        )
    return sets


class EvidenceClass(StrEnum):
    DIRECT = "direct"
    CONTEXTUAL = "contextual"
    NO_CHANGE = "no_change"
    MISSING = "missing"
    UNKNOWN = "unknown"


class Classification(StrictModel):
    evidence_class: EvidenceClass
    reason: str


class RefIndex:
    """The references an agent may cite, the evidence sources each resolves to, and what each shows."""

    def __init__(self) -> None:
        self._sources: dict[str, frozenset[EvidenceSource]] = {}
        self._set_outcome: dict[str, EvidenceOutcome] = {}
        self._records: dict[str, tuple[EvidenceSource | None, dict[str, Any]]] = {}
        self._finding_kind: dict[str, str] = {}
        self._finding_refs: dict[str, list[str]] = {}
        self._series: dict[tuple[str, str], list[tuple[str, float]]] = {}

    def add_sets(self, sets: Iterable[EvidenceSet]) -> "RefIndex":
        for s in sets:
            sources = frozenset({s.source}) if s.source else frozenset()
            self._sources[s.ref] = sources
            self._set_outcome[s.ref] = s.outcome
            for item in s.items:
                self._sources[item.ref] = sources
                self._records[item.ref] = (s.source, item.record)
                if s.source is EvidenceSource.METRICS:
                    r = item.record
                    self._series.setdefault((r["service"], r["metric_name"]), []).append((r["timestamp"], r["value"]))
        return self

    def add_finding(self, ref: str, kind: str, evidence_refs: Iterable[str]) -> None:
        refs = list(evidence_refs)
        sources: set[EvidenceSource] = set()
        for r in refs:
            sources |= self._sources.get(r, frozenset())
        self._sources[ref] = frozenset(sources)
        self._finding_kind[ref] = kind
        self._finding_refs[ref] = refs

    def __contains__(self, ref: str) -> bool:
        return ref in self._sources

    def unknown(self, refs: Iterable[str]) -> list[str]:
        return [r for r in refs if r not in self._sources]

    def resolve(self, ref: str) -> tuple[str | None, list[str]]:
        """Return (canonical reference or None, candidates).

        A reference shown to the agent resolves to itself. A bare finding id such as "dep-3"
        resolves only if exactly one finding shown to this agent has that id (e.g.
        "dependency/dep-3"). Record and result references are never guessed, so anything else,
        including an id matching several findings, resolves to None.
        """
        if ref in self._sources:
            return ref, [ref]
        if "/" in ref or "#" in ref:
            return None, []
        candidates = sorted(r for r in self._finding_kind if r.split("/", 1)[1] == ref)
        return (candidates[0] if len(candidates) == 1 else None), candidates

    def sources(self, refs: Iterable[str]) -> set[EvidenceSource]:
        found: set[EvidenceSource] = set()
        for r in refs:
            found |= self._sources.get(r, frozenset())
        return found

    def unusable_as_support(self, refs: Iterable[str]) -> list[str]:
        """Whole-result refs whose tool failed or never ran: they show a gap, not evidence either way."""
        return [r for r in refs if self._set_outcome.get(r) in UNUSABLE_AS_SUPPORT]

    def is_direct_evidence(self, ref: str) -> bool:
        """An incident-specific observation (see the module docstring); topology and history are not."""
        return self.classify(ref).evidence_class is EvidenceClass.DIRECT

    def direct_sources(self, refs: Iterable[str]) -> set[EvidenceSource]:
        """Evidence sources behind the direct references only (see app.agents.independence for corroboration)."""
        return self.sources(self._direct_leaves(refs))

    def direct_leaves(self, refs: Iterable[str]) -> list[str]:
        """Direct tool-record references behind ``refs``, following fact findings to their records."""
        return list(dict.fromkeys(self._direct_leaves(refs)))

    def record(self, ref: str) -> tuple[EvidenceSource | None, dict[str, Any]] | None:
        return self._records.get(ref)

    def records(self) -> list[tuple[str, EvidenceSource | None, dict[str, Any]]]:
        return [(ref, source, r) for ref, (source, r) in self._records.items()]

    def outcome(self, set_ref: str) -> EvidenceOutcome | None:
        return self._set_outcome.get(set_ref)

    def classify(self, ref: str, _depth: int = 0) -> Classification:
        if ref in self._set_outcome:
            outcome = self._set_outcome[ref]
            if outcome in UNUSABLE_AS_SUPPORT:
                return Classification(evidence_class=EvidenceClass.MISSING, reason=f"the {ref} query {outcome}; nothing was observed")
            if outcome is EvidenceOutcome.NO_MATCHES:
                return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason=f"the {ref} query matched nothing (not proof of absence)")
            return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason=f"{ref} is a whole result, not a specific observation")
        if ref in self._records:
            return self._classify_record(ref)
        if ref in self._finding_kind and _depth < 3:
            if self._finding_kind[ref] != "fact":
                return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason="an interpretation, not an observation")
            inner = [self.classify(r, _depth + 1) for r in self._finding_refs.get(ref, [])]
            for wanted in (EvidenceClass.DIRECT, EvidenceClass.NO_CHANGE, EvidenceClass.CONTEXTUAL):
                hits = [c for c in inner if c.evidence_class is wanted]
                if hits and (wanted is not EvidenceClass.NO_CHANGE or len(hits) == len(inner)):
                    return Classification(evidence_class=wanted, reason=f"finding based on: {hits[0].reason}")
            return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason="finding with no classifiable evidence")
        return Classification(evidence_class=EvidenceClass.UNKNOWN, reason="not among the evidence shown")

    def _classify_record(self, ref: str) -> Classification:
        source, r = self._records[ref]
        if source is EvidenceSource.LOGS:
            return Classification(evidence_class=EvidenceClass.DIRECT, reason=f"log line from {r['service']} during the window")
        if source is EvidenceSource.CHANGES:
            return Classification(evidence_class=EvidenceClass.DIRECT, reason=f"change {r['change_id']} occurred (timing only, not causation)")
        if source is EvidenceSource.DEPENDENCIES:
            return Classification(
                evidence_class=EvidenceClass.CONTEXTUAL,
                reason=f"{r['service']} depends on {r['depends_on']}: a relationship, not evidence that it failed or was slow",
            )
        if source is EvidenceSource.HISTORICAL_INCIDENTS:
            return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason="a past incident, not an observation of this one")
        if source is EvidenceSource.METRICS:
            return self._classify_metric(r)
        return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason="unclassified record")

    def _classify_metric(self, r: dict[str, Any]) -> Classification:
        name = f"{r['service']} {r['metric_name']}"
        points = sorted(self._series.get((r["service"], r["metric_name"]), []))
        if len(points) < 3:
            return Classification(evidence_class=EvidenceClass.CONTEXTUAL, reason=f"{name}: too few samples to compare")
        head = sorted(v for _, v in points[: max(1, len(points) // 3)])
        baseline = _median(head)
        # Robust noise estimate: scaled median absolute deviation, but never below the smallest step between
        # distinct baseline values (quantised series such as an error rate alternating 0.2% / 0.3%).
        mad = 1.4826 * _median(sorted(abs(v - baseline) for v in head))
        steps = [b - a for a, b in zip(sorted(set(head)), sorted(set(head))[1:])]
        noise = max(mad, min(steps, default=0.0))
        value = r["value"]
        change = metric_change(value, baseline)
        detail = f"{name} = {value:g} vs baseline {baseline:.3g} ({change:+.0%})"
        if abs(value - baseline) >= max(CHANGE_THRESHOLD * abs(baseline), NOISE_SIGMAS * noise) and abs(value - baseline) > 0:
            return Classification(evidence_class=EvidenceClass.DIRECT, reason=f"{detail}: changed")
        return Classification(evidence_class=EvidenceClass.NO_CHANGE, reason=f"{detail}: within the normal range")

    def _direct_leaves(self, refs: Iterable[str], depth: int = 0) -> list[str]:
        leaves = []
        for ref in refs:
            if ref in self._finding_refs and depth < 3 and self._finding_kind.get(ref) == "fact":
                leaves += self._direct_leaves(self._finding_refs[ref], depth + 1)
            elif self.is_direct_evidence(ref):
                leaves.append(ref)
        return leaves


def _median(sorted_values: list[float]) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    mid = n // 2
    return sorted_values[mid] if n % 2 else (sorted_values[mid - 1] + sorted_values[mid]) / 2


def metric_change(value: float, baseline: float) -> float:
    """Relative change from baseline; a change from (near) zero counts as a full change."""
    if abs(baseline) < 1e-9:
        return 0.0 if abs(value) < 1e-9 else 1.0
    return (value - baseline) / abs(baseline)


def duplicate_ids(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dupes = []
    for i in ids:
        if i in seen:
            dupes.append(i)
        seen.add(i)
    return dupes
