"""Evidence references shared by every agent.

Reference formats. Each is valid only if the agent was actually shown it:

    "<task_id>"            a whole evidence result; cite it for gaps, absence or failure
    "<task_id>#<n>"        record n (0-based) of that result, e.g. "ev.logs#3"
    "<task_id>/<id>"       a finding produced by an agent task, e.g. "observability/obs-2"

Every reference resolves back to evidence tool results, so provenance is never lost.
"""

from collections.abc import Iterable, Mapping
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
            )
        )
    return sets


class RefIndex:
    """The references an agent may cite, and the evidence sources each one resolves to."""

    def __init__(self) -> None:
        self._sources: dict[str, frozenset[EvidenceSource]] = {}
        self._set_outcome: dict[str, EvidenceOutcome] = {}
        self._record_refs: set[str] = set()
        self._finding_kind: dict[str, str] = {}

    def add_sets(self, sets: Iterable[EvidenceSet]) -> "RefIndex":
        for s in sets:
            sources = frozenset({s.source}) if s.source else frozenset()
            self._sources[s.ref] = sources
            self._set_outcome[s.ref] = s.outcome
            for item in s.items:
                self._sources[item.ref] = sources
                self._record_refs.add(item.ref)
        return self

    def add_finding(self, ref: str, kind: str, evidence_refs: Iterable[str]) -> None:
        sources: set[EvidenceSource] = set()
        for r in evidence_refs:
            sources |= self._sources.get(r, frozenset())
        self._sources[ref] = frozenset(sources)
        self._finding_kind[ref] = kind

    def __contains__(self, ref: str) -> bool:
        return ref in self._sources

    def unknown(self, refs: Iterable[str]) -> list[str]:
        return [r for r in refs if r not in self._sources]

    def sources(self, refs: Iterable[str]) -> set[EvidenceSource]:
        found: set[EvidenceSource] = set()
        for r in refs:
            found |= self._sources.get(r, frozenset())
        return found

    def unusable_as_support(self, refs: Iterable[str]) -> list[str]:
        """Whole-result refs whose tool failed or never ran: they show a gap, not evidence either way."""
        return [r for r in refs if self._set_outcome.get(r) in UNUSABLE_AS_SUPPORT]

    def is_direct_evidence(self, ref: str) -> bool:
        """A tool record, or an agent finding labelled as an observed fact."""
        return ref in self._record_refs or self._finding_kind.get(ref) == "fact"

    def outcome(self, set_ref: str) -> EvidenceOutcome | None:
        return self._set_outcome.get(set_ref)


def duplicate_ids(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dupes = []
    for i in ids:
        if i in seen:
            dupes.append(i)
        seen.add(i)
    return dupes
