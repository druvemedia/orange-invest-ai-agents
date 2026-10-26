from collections.abc import Sequence

from app.schemas.common import TimeRange
from app.schemas.evidence import DependencyRecord, EvidenceSource
from app.schemas.queries import DependencyQuery
from app.tools.base import EvidenceTool


class DependencyTool(EvidenceTool[DependencyQuery, DependencyRecord]):
    name = "get_service_dependencies"
    description = (
        "Return dependency edges around a service: what it depends on (downstream), what "
        "depends on it (upstream), or both, up to `depth` hops. Each record is one edge."
    )
    source = EvidenceSource.DEPENDENCIES
    dataset = "dependencies.jsonl"
    query_model = DependencyQuery
    record_model = DependencyRecord

    def _select(self, query: DependencyQuery, records: Sequence[DependencyRecord]) -> list[DependencyRecord]:
        selected: dict[tuple[str, str], DependencyRecord] = {}
        if query.direction in ("downstream", "both"):
            self._walk(query, records, selected, downstream=True)
        if query.direction in ("upstream", "both"):
            self._walk(query, records, selected, downstream=False)
        return sorted(selected.values(), key=lambda r: (r.service, r.depends_on))

    @staticmethod
    def _walk(
        query: DependencyQuery,
        records: Sequence[DependencyRecord],
        selected: dict[tuple[str, str], DependencyRecord],
        downstream: bool,
    ) -> None:
        # Walks one direction only, so "both" never mixes in siblings (e.g. callers of a dependency).
        visited = {query.service}
        frontier = {query.service}
        for _ in range(query.depth):
            next_frontier: set[str] = set()
            for r in records:
                near, far = (r.service, r.depends_on) if downstream else (r.depends_on, r.service)
                if near in frontier:
                    selected[(r.service, r.depends_on)] = r
                    next_frontier.add(far)
            frontier = next_frontier - visited
            visited |= frontier

    def _extra_warnings(self, query: DependencyQuery, records: Sequence[DependencyRecord]) -> list[str]:
        known = {r.service for r in records} | {r.depends_on for r in records}
        if query.service not in known:
            return [
                f"Service '{query.service}' is not in the dependency catalog; "
                "its dependencies are unknown, not necessarily absent."
            ]
        return []

    def _scope(self, query: DependencyQuery) -> tuple[list[str], TimeRange | None]:
        return [query.service], None
