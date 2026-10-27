"""Evidence references shared by every agent, and what each cited reference actually shows.

Reference formats. Each is valid only if the agent was actually shown it:

    "<task_id>"            a whole evidence result; cite it for gaps, absence or failure
    "<task_id>#<n>"        record n (0-based) of that result, e.g. "ev.logs#3"
    "<task_id>/<id>"       a finding produced by an agent task, e.g. "observability/obs-2"

Every reference resolves back to evidence tool results, so provenance is never lost.

Evidence classes (deterministic, see ``RefIndex.classify``):
    direct      an incident-specific observation: a log line, a change record, or a metric sample that
                deviates from its own series' pre-incident baseline by more than its noise
    contextual  true but not evidence of failure in this incident: a dependency edge (a relationship),
                a past incident, an interpretation, a whole result, or a metric that cannot be compared
                (too few samples, or too few before the estimated onset: an insufficient baseline)
    no_change   a metric sample within the normal range of its series: it cannot show an increase or drop
    missing     a result whose tool failed or never ran: it shows a gap, not evidence either way

Metric baseline. A metric sample is compared with the samples of its series taken before the estimated
incident onset, never with the incident's own readings. The onset estimate deliberately ignores metrics (it
would otherwise depend on the classification it feeds): it is the first ERROR/CRITICAL log of an episode
(one followed by another within EPISODE_GAP) on the incident's affected services, or on any service if none
are known; or the incident's reported time if that is earlier or no episode is found. Unlike the timeline
check, a lone error is not an episode here, so one stray error cannot empty the baseline. A metric can
depart a few minutes before the first error log; the median keeps such boundary samples from shifting the
baseline. Fewer than MIN_BASELINE_SAMPLES samples before the onset give an insufficient baseline
(contextual), not "no change". Without any onset information (no reported time and no error logs) the earliest
third of the retrieved samples is used, with the same minimum. Evidence planners fetch metrics from at least
METRIC_BASELINE_LOOKBACK before the reported time so that a baseline exists.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field

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
# A metric sample is a change only if it differs from its series' baseline (the median of its samples before
# the estimated onset) by at least this fraction AND by at least NOISE_SIGMAS times the baseline noise.
CHANGE_THRESHOLD = 0.25
NOISE_SIGMAS = 3.0
MIN_BASELINE_SAMPLES = 3
# Metric queries start at least this long before the reported time, so pre-incident samples are retrieved.
METRIC_BASELINE_LOOKBACK = timedelta(minutes=60)
# Onset is the first symptom followed by another within this gap; isolated earlier symptoms are ignored.
EPISODE_GAP = timedelta(minutes=30)
SYMPTOM_LEVELS = frozenset({"ERROR", "CRITICAL"})
# Task input keys under which evidence planners record the incident's reported time (ISO 8601) and its
# affected services, which bound the onset estimate.
REPORTED_AT_INPUT = "incident_reported_at"
AFFECTED_SERVICES_INPUT = "incident_affected_services"


def parse_instant(value: Any) -> datetime | None:
    """An aware datetime in UTC, or None if missing, unparseable or without a timezone."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


def metric_query_start(window_start: datetime, reported_at: datetime | None) -> datetime:
    """Start a metric query early enough to include a pre-incident baseline."""
    return min(window_start, reported_at - METRIC_BASELINE_LOOKBACK) if reported_at else window_start


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
    # Bound the onset for metric baselines. Code-only: excluded from what the model is shown.
    incident_reported_at: str | None = Field(default=None, exclude=True)
    incident_affected_services: list[str] | None = Field(default=None, exclude=True)


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
                incident_reported_at=record.spec.inputs.get(REPORTED_AT_INPUT),
                incident_affected_services=record.spec.inputs.get(AFFECTED_SERVICES_INPUT),
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
        # Per series: sample time -> value. Keyed by time, so a sample retrieved twice counts once.
        self._series: dict[tuple[str, str], dict[datetime, float]] = {}
        self._reported_at: datetime | None = None
        self._affected: frozenset[str] | None = None
        self._error_times: dict[str, set[datetime]] = {}  # per service

    def add_sets(self, sets: Iterable[EvidenceSet]) -> "RefIndex":
        for s in sets:
            sources = frozenset({s.source}) if s.source else frozenset()
            self._sources[s.ref] = sources
            self._set_outcome[s.ref] = s.outcome
            if self._reported_at is None:
                self._reported_at = parse_instant(s.incident_reported_at)
            if self._affected is None and s.incident_affected_services is not None:
                self._affected = frozenset(s.incident_affected_services)
            for item in s.items:
                self._sources[item.ref] = sources
                self._records[item.ref] = (s.source, item.record)
                r = item.record
                t = parse_instant(r.get("timestamp"))
                if s.source is EvidenceSource.METRICS and t:
                    self._series.setdefault((r["service"], r["metric_name"]), {}).setdefault(t, r["value"])
                elif s.source is EvidenceSource.LOGS and t and r.get("level") in SYMPTOM_LEVELS:
                    self._error_times.setdefault(r.get("service"), set()).add(t)
        return self

    def onset_estimate(self) -> tuple[datetime | None, str]:
        """The estimated incident onset that bounds metric baselines, and what it is based on (see module doc)."""
        services = self._affected or self._error_times.keys()  # unknown affected services: every service counts
        episode = _episode_start(sorted(set().union(*(self._error_times.get(s, set()) for s in services))))
        if episode and (self._reported_at is None or episode < self._reported_at):
            return episode, "the first ERROR log of the episode"
        if self._reported_at:
            return self._reported_at, "the reported time"
        return None, "unknown"

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

    def metric_baseline(self, service: str, metric: str) -> tuple[list[float] | None, str]:
        """The sorted baseline samples of a series and how they were chosen, or (None, why) if there are too few."""
        points = sorted(self._series.get((service, metric), {}).items())
        onset, basis = self.onset_estimate()
        if onset is None:
            head = sorted(v for _, v in points[: len(points) // 3])
            when = "in the earliest third of the retrieved samples (onset unknown)"
        else:
            head = sorted(v for t, v in points if t < onset)
            when = f"before the estimated onset {onset:%Y-%m-%dT%H:%M:%SZ}, {basis}"
        if len(head) < MIN_BASELINE_SAMPLES:
            return None, f"insufficient baseline ({len(head)} sample(s) {when})"
        return head, f"{len(head)} samples {when}"

    def _classify_metric(self, r: dict[str, Any]) -> Classification:
        name = f"{r['service']} {r['metric_name']}"
        head, how = self.metric_baseline(r["service"], r["metric_name"])
        if head is None:
            return Classification(evidence_class=EvidenceClass.CONTEXTUAL,
                                  reason=f"{name} = {r['value']:g}: {how}, so it cannot show a change or its absence")
        baseline = _median(head)
        # Robust noise estimate: scaled median absolute deviation, but never below the smallest step between
        # distinct baseline values (quantised series such as an error rate alternating 0.2% / 0.3%). Steps are taken
        # within the middle half of the baseline only, so one boundary sample that already shows the incident (run 4:
        # nine samples at 0.2% and one at 7.61%) cannot make a 37x jump look like noise.
        mad = 1.4826 * _median(sorted(abs(v - baseline) for v in head))
        core = sorted({v for v in head if head[len(head) // 4] <= v <= head[(3 * len(head)) // 4]})
        steps = [b - a for a, b in zip(core, core[1:])]
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


def _episode_start(times: list[datetime]) -> datetime | None:
    """The first of ``times`` (sorted) followed by another within EPISODE_GAP. A lone time is not an episode."""
    for i, t in enumerate(times[:-1]):
        if times[i + 1] - t <= EPISODE_GAP:
            return t
    return None


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
