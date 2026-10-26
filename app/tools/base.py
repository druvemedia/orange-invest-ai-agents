"""Shared behaviour for all evidence tools.

``EvidenceTool.run`` is the single entry point. It never raises for expected
problems (bad input, injected faults, missing or malformed data); those come
back as an ``EvidenceResult`` with ``completeness`` and ``warnings``/``errors``
set, so a caller can always tell "no evidence found" from "could not look".
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import ValidationError

from app.schemas.common import StrictModel, TimeRange
from app.schemas.evidence import (
    Completeness,
    CoverageGap,
    EvidenceResult,
    EvidenceSource,
    ToolError,
    ToolErrorCode,
)
from app.schemas.queries import EvidenceQuery
from app.tools.datastore import (
    DatasetUnavailableError,
    FixtureStore,
    summarize_validation_error,
)
from app.tools.faults import FaultInjector, simulated_error

logger = logging.getLogger(__name__)

COVERAGE_GAPS_DATASET = "coverage_gaps.jsonl"

ABSENCE_NOTE = (
    "No records matched this query. Absence of records in the available data "
    "does not prove that the event did not occur."
)

QueryT = TypeVar("QueryT", bound=EvidenceQuery)
RecordT = TypeVar("RecordT", bound=StrictModel)


class EvidenceTool(ABC, Generic[QueryT, RecordT]):
    name: ClassVar[str]
    description: ClassVar[str]
    source: ClassVar[EvidenceSource]
    dataset: ClassVar[str]
    query_model: ClassVar[type[EvidenceQuery]]
    record_model: ClassVar[type[StrictModel]]

    def __init__(self, store: FixtureStore, faults: FaultInjector | None = None) -> None:
        self._store = store
        self._faults = faults or FaultInjector()

    @abstractmethod
    def _select(self, query: QueryT, records: Sequence[RecordT]) -> list[RecordT]:
        """Return every matching record, in a deterministic order."""

    @abstractmethod
    def _scope(self, query: QueryT) -> tuple[list[str], TimeRange | None]:
        """Services and window the query covers, used to find relevant coverage gaps."""

    def _extra_warnings(self, query: QueryT, records: Sequence[RecordT]) -> list[str]:
        return []

    def run(self, query: QueryT | Mapping[str, Any]) -> EvidenceResult[RecordT]:
        try:
            parsed: QueryT = self.query_model.model_validate(query)  # type: ignore[assignment]
        except ValidationError as exc:
            raw = dict(query) if isinstance(query, Mapping) else {"value": repr(query)}
            return self._failed(
                raw,
                ToolError(
                    code=ToolErrorCode.INVALID_QUERY,
                    message=f"Invalid query: {summarize_validation_error(exc)}",
                    retryable=False,
                ),
            )
        echo = parsed.model_dump(mode="json")

        fault = self._faults.fault_for(self.name)
        if fault is not None:
            return self._failed(echo, simulated_error(fault))

        try:
            dataset = self._store.load(self.dataset, self.record_model)
        except DatasetUnavailableError as exc:
            return self._failed(
                echo, ToolError(code=ToolErrorCode.DATA_UNAVAILABLE, message=str(exc), retryable=False)
            )

        matches = self._select(parsed, dataset.records)  # type: ignore[arg-type]
        warnings = self._extra_warnings(parsed, dataset.records)  # type: ignore[arg-type]
        partial = False

        if dataset.issues:
            partial = True
            warnings.append(
                f"{len(dataset.issues)} malformed record(s) in '{self.dataset}' were skipped, "
                f"so matching records may be missing. First issue: {dataset.issues[0]}"
            )

        gaps, gap_warnings = self._relevant_gaps(parsed)
        warnings.extend(gap_warnings)
        for gap in gaps:
            partial = True
            warnings.append(
                f"Known coverage gap in {self.source} for {gap.service or 'all services'} "
                f"from {gap.start.isoformat()} to {gap.end.isoformat()}: {gap.reason}"
            )

        truncated = len(matches) > parsed.limit
        if truncated:
            partial = True
            warnings.append(
                f"Returned the first {parsed.limit} of {len(matches)} matching records; "
                "narrow the query or raise the limit."
            )
            matches = matches[: parsed.limit]

        if not matches:
            warnings.append(ABSENCE_NOTE)

        result = EvidenceResult[self.record_model](  # type: ignore[name-defined]
            source=self.source,
            query=echo,
            records=matches,
            completeness=Completeness.PARTIAL if partial else Completeness.COMPLETE,
            truncated=truncated,
            warnings=warnings,
        )
        logger.info("tool=%s status=%s records=%d", self.name, result.status, result.record_count)
        return result

    def _relevant_gaps(self, query: QueryT) -> tuple[list[CoverageGap], list[str]]:
        try:
            gaps = self._store.load(COVERAGE_GAPS_DATASET, CoverageGap)
        except DatasetUnavailableError:
            return [], []
        warnings = []
        if gaps.issues:
            warnings.append(
                f"{len(gaps.issues)} coverage-gap entries could not be read; "
                "known data gaps may be under-reported."
            )
        services, window = self._scope(query)
        relevant = [
            gap
            for gap in gaps.records
            if gap.source is self.source
            and (gap.service is None or not services or gap.service in services)
            and (window is None or window.overlaps(TimeRange(start=gap.start, end=gap.end)))
        ]
        return relevant, warnings

    def _failed(self, query: dict[str, Any], error: ToolError) -> EvidenceResult[RecordT]:
        logger.warning("tool=%s failed code=%s simulated=%s", self.name, error.code, error.simulated)
        return EvidenceResult[self.record_model](  # type: ignore[name-defined]
            source=self.source,
            query=query,
            completeness=Completeness.NONE,
            errors=[error],
        )
