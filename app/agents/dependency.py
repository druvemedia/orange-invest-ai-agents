"""Dependency agent: relates changes and service dependencies to the incident, in time and topology."""

from collections import deque
from datetime import datetime

from app.agents.base import LLMAgent, Prepared, require_scope, unknown_ref_problems
from app.agents.contracts import (
    ChangeRelation,
    DependencyInput,
    DependencyOutput,
    DependencyResult,
    InvestigationScope,
)
from app.agents.evidence import EvidenceSet, RefIndex, duplicate_ids, evidence_sets
from app.agents.observability import data_gaps
from app.orchestration import TaskContext
from app.schemas.evidence import EvidenceSource


class DependencyAgent(LLMAgent[DependencyInput, DependencyOutput, DependencyResult]):
    name = "dependency"
    kind = "agent.dependency"
    output_model = DependencyOutput
    instructions = """
You are the Dependency agent in an incident investigation team. You receive deployment and
configuration history, service dependency edges, and change relations computed in code
(minutes before the report, and how each changed service relates to the focus services).

Return findings, each with a unique id "dep-1", "dep-2", ...:
- kind "fact": directly visible in cited records (a change at a time, an edge between services).
- kind "interpretation": e.g. "the config change on X shortly before onset is a candidate trigger".
Every finding cites at least one reference. Say explicitly when a relation is only temporal or
structural; never claim that a change caused the incident. Note services whose change history or
dependency data is missing, and treat those gaps as unknowns.
""".strip()

    def prepare(self, ctx: TaskContext) -> Prepared[DependencyInput]:
        scope = require_scope(ctx.dependencies)
        sets = evidence_sets(ctx.dependencies)
        return Prepared(
            input=DependencyInput(
                incident=ctx.incident,
                scope=scope,
                evidence=sets,
                change_relations=relate_changes(sets, scope, ctx.incident.reported_at),
            ),
            refs=RefIndex().add_sets(sets),
            consumed=sorted(ctx.dependencies),
        )

    def without_model(self, ctx, prepared):
        if any(s.items for s in prepared.input.evidence):
            return None
        return DependencyOutput(
            findings=[],
            evidence_gaps=data_gaps(prepared.input.evidence),
            summary="No change or dependency records were obtained; nothing can be related.",
        )

    def check(self, output: DependencyOutput, prepared: Prepared[DependencyInput]) -> list[str]:
        problems = []
        if any(s.items for s in prepared.input.evidence) and not output.findings:
            problems.append("no findings although evidence records exist")
        problems += [f"duplicate finding id '{d}'" for d in duplicate_ids(f.finding_id for f in output.findings)]
        for f in output.findings:
            if not f.finding_id.startswith("dep-"):
                problems.append(f"finding id '{f.finding_id}' must look like 'dep-1'")
            if not f.evidence_refs:
                problems.append(f"{f.finding_id} cites no evidence")
            problems += unknown_ref_problems(f.finding_id, f.evidence_refs, prepared.refs)
            if f.kind == "fact":
                bad = prepared.refs.unusable_as_support(f.evidence_refs)
                problems += [f"{f.finding_id} states a fact from missing evidence '{r}'" for r in bad]
        return problems

    def finish(self, ctx, output, prepared):
        result = DependencyResult(
            output=output,
            change_relations=prepared.input.change_relations,
            data_gaps=data_gaps(prepared.input.evidence),
        )
        return result, [], []


def relate_changes(
    sets: list[EvidenceSet], scope: InvestigationScope, reported_at: datetime | None
) -> list[ChangeRelation]:
    downstream: dict[str, set[str]] = {}
    upstream: dict[str, set[str]] = {}
    dependency_data = False
    for s in sets:
        if s.source is EvidenceSource.DEPENDENCIES and s.items:
            dependency_data = True
            for item in s.items:
                a, b = item.record["service"], item.record["depends_on"]
                downstream.setdefault(a, set()).add(b)
                upstream.setdefault(b, set()).add(a)

    relations = []
    for s in sets:
        if s.source is not EvidenceSource.CHANGES:
            continue
        for item in s.items:
            r = item.record
            timestamp = datetime.fromisoformat(r["timestamp"])
            relation, path = _relation(r["service"], scope.focus_services, downstream, upstream, dependency_data)
            relations.append(
                ChangeRelation(
                    change_ref=item.ref,
                    change_id=r["change_id"],
                    service=r["service"],
                    change_type=r["change_type"],
                    timestamp=timestamp,
                    minutes_before_report=(
                        round((reported_at - timestamp).total_seconds() / 60, 1) if reported_at else None
                    ),
                    relation=relation,
                    path=path,
                )
            )
    return sorted(relations, key=lambda c: c.timestamp)


def _relation(service, focus, downstream, upstream, dependency_data):
    if service in focus:
        return "focus_service", [service]
    if not focus or not dependency_data:
        return "unknown", []
    for start in focus:
        if path := _path(start, service, downstream):
            return "dependency_of_focus", path
    for start in focus:
        if path := _path(start, service, upstream):
            return "depends_on_focus", path
    return "no_known_relation", []


def _path(start: str, goal: str, edges: dict[str, set[str]]) -> list[str]:
    queue = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        for nxt in sorted(edges.get(path[-1], ())):
            if nxt == goal:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return []
