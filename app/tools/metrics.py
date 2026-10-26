from collections.abc import Sequence

from app.schemas.common import TimeRange
from app.schemas.evidence import EvidenceSource, MetricRecord
from app.schemas.queries import MetricQuery
from app.tools.base import EvidenceTool


class MetricQueryTool(EvidenceTool[MetricQuery, MetricRecord]):
    name = "query_metrics"
    description = (
        "Fetch raw performance metric samples (e.g. latency_p95_ms, error_rate_percent) in a "
        "time window, filtered by services and metric names. Returns samples grouped by "
        "service and metric, oldest first. No aggregation or anomaly judgement is applied."
    )
    source = EvidenceSource.METRICS
    dataset = "metrics.jsonl"
    query_model = MetricQuery
    record_model = MetricRecord

    def _select(self, query: MetricQuery, records: Sequence[MetricRecord]) -> list[MetricRecord]:
        window = query.window
        services = set(query.services)
        names = set(query.metric_names)
        matches = [
            r
            for r in records
            if window.contains(r.timestamp)
            and (not services or r.service in services)
            and (not names or r.metric_name in names)
        ]
        return sorted(matches, key=lambda r: (r.service, r.metric_name, r.timestamp))

    def _scope(self, query: MetricQuery) -> tuple[list[str], TimeRange | None]:
        return query.services, query.window
