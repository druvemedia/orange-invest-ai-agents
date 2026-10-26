"""Deterministic evidence tools backed by local fixture data.

Use ``build_evidence_tools()`` to get one instance of each tool, keyed by tool name.
"""

from app.tools.base import EvidenceTool
from app.tools.changes import ChangeHistoryTool
from app.tools.datastore import DEFAULT_FIXTURES_DIR, FixtureStore
from app.tools.dependencies import DependencyTool
from app.tools.faults import FaultInjector, SimulatedFault
from app.tools.historical_incidents import HistoricalIncidentTool
from app.tools.logs import LogSearchTool
from app.tools.metrics import MetricQueryTool

TOOL_CLASSES: tuple[type[EvidenceTool], ...] = (
    LogSearchTool,
    MetricQueryTool,
    ChangeHistoryTool,
    DependencyTool,
    HistoricalIncidentTool,
)


def build_evidence_tools(
    store: FixtureStore | None = None, faults: FaultInjector | None = None
) -> dict[str, EvidenceTool]:
    store = store or FixtureStore()
    return {cls.name: cls(store, faults) for cls in TOOL_CLASSES}


__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "TOOL_CLASSES",
    "ChangeHistoryTool",
    "DependencyTool",
    "EvidenceTool",
    "FaultInjector",
    "FixtureStore",
    "HistoricalIncidentTool",
    "LogSearchTool",
    "MetricQueryTool",
    "SimulatedFault",
    "build_evidence_tools",
]
