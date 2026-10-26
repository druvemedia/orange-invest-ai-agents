from collections.abc import Sequence

from app.schemas.common import TimeRange
from app.schemas.evidence import EvidenceSource, HistoricalIncidentRecord
from app.schemas.queries import HistoricalIncidentQuery
from app.tools.base import EvidenceTool


class HistoricalIncidentTool(EvidenceTool[HistoricalIncidentQuery, HistoricalIncidentRecord]):
    name = "search_historical_incidents"
    description = (
        "Search past, closed incidents by keywords, services, severities and date range. "
        "Results are ordered by how many distinct keywords matched, then most recent first. "
        "A past root cause describes that incident only, not the current one."
    )
    source = EvidenceSource.HISTORICAL_INCIDENTS
    dataset = "historical_incidents.jsonl"
    query_model = HistoricalIncidentQuery
    record_model = HistoricalIncidentRecord

    def _select(
        self, query: HistoricalIncidentQuery, records: Sequence[HistoricalIncidentRecord]
    ) -> list[HistoricalIncidentRecord]:
        services = set(query.services)
        severities = set(query.severities)
        keywords = [k.lower() for k in query.keywords]
        scored: list[tuple[int, HistoricalIncidentRecord]] = []
        for r in records:
            if services and not services.intersection(r.services):
                continue
            if severities and r.severity not in severities:
                continue
            if query.occurred_after and r.occurred_at < query.occurred_after:
                continue
            if query.occurred_before and r.occurred_at > query.occurred_before:
                continue
            text = _searchable_text(r)
            score = sum(1 for k in keywords if k in text)
            if keywords and score == 0:
                continue
            scored.append((score, r))
        scored.sort(key=lambda item: (-item[0], -item[1].occurred_at.timestamp(), item[1].incident_id))
        return [r for _, r in scored]

    def _scope(self, query: HistoricalIncidentQuery) -> tuple[list[str], TimeRange | None]:
        return query.services, None


def _searchable_text(record: HistoricalIncidentRecord) -> str:
    parts = [record.title, *record.symptoms, *record.tags, record.root_cause or "", record.resolution or ""]
    return " ".join(parts).lower()
