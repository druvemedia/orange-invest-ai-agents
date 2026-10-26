from collections.abc import Sequence

from app.schemas.common import TimeRange
from app.schemas.evidence import EvidenceSource, LogRecord
from app.schemas.queries import LogSearchQuery
from app.tools.base import EvidenceTool


class LogSearchTool(EvidenceTool[LogSearchQuery, LogRecord]):
    name = "search_logs"
    description = (
        "Search application logs in a time window. Filter by services, minimum log level, "
        "message keywords (any match, case-insensitive) or trace ID. Returns records oldest first."
    )
    source = EvidenceSource.LOGS
    dataset = "logs.jsonl"
    query_model = LogSearchQuery
    record_model = LogRecord

    def _select(self, query: LogSearchQuery, records: Sequence[LogRecord]) -> list[LogRecord]:
        window = query.window
        services = set(query.services)
        keywords = [k.lower() for k in query.keywords]
        min_rank = query.min_level.rank
        matches = [
            r
            for r in records
            if window.contains(r.timestamp)
            and (not services or r.service in services)
            and r.level.rank >= min_rank
            and (query.trace_id is None or r.trace_id == query.trace_id)
            and (not keywords or any(k in r.message.lower() for k in keywords))
        ]
        return sorted(matches, key=lambda r: r.timestamp)

    def _scope(self, query: LogSearchQuery) -> tuple[list[str], TimeRange | None]:
        return query.services, query.window
