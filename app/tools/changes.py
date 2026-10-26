from collections.abc import Sequence

from app.schemas.common import TimeRange
from app.schemas.evidence import ChangeRecord, EvidenceSource
from app.schemas.queries import ChangeHistoryQuery
from app.tools.base import EvidenceTool


class ChangeHistoryTool(EvidenceTool[ChangeHistoryQuery, ChangeRecord]):
    name = "get_change_history"
    description = (
        "List deployments, configuration changes, feature-flag changes and rollbacks in a "
        "time window, filtered by services and change types. Returns changes oldest first."
    )
    source = EvidenceSource.CHANGES
    dataset = "changes.jsonl"
    query_model = ChangeHistoryQuery
    record_model = ChangeRecord

    def _select(self, query: ChangeHistoryQuery, records: Sequence[ChangeRecord]) -> list[ChangeRecord]:
        window = query.window
        services = set(query.services)
        types = set(query.change_types)
        matches = [
            r
            for r in records
            if window.contains(r.timestamp)
            and (not services or r.service in services)
            and (not types or r.change_type in types)
        ]
        return sorted(matches, key=lambda r: (r.timestamp, r.change_id))

    def _scope(self, query: ChangeHistoryQuery) -> tuple[list[str], TimeRange | None]:
        return query.services, query.window
