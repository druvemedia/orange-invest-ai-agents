"""Deterministic failure simulation for evidence tools.

Faults are configured per tool name by the caller (normally a test or an
evaluation scenario). Nothing sleeps and nothing touches the network: an
injected timeout returns immediately with an error marked ``simulated=True``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.schemas.evidence import ToolError, ToolErrorCode


class SimulatedFault(StrEnum):
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FaultInjector:
    """Maps tool names (e.g. ``"search_logs"``) to the fault every call to that tool should return."""

    faults: Mapping[str, SimulatedFault] = field(default_factory=dict)

    def fault_for(self, tool_name: str) -> SimulatedFault | None:
        return self.faults.get(tool_name)


def simulated_error(fault: SimulatedFault) -> ToolError:
    if fault is SimulatedFault.TIMEOUT:
        return ToolError(
            code=ToolErrorCode.TIMEOUT,
            message="Simulated timeout injected by the fault plan; no external call was made.",
            retryable=True,
            simulated=True,
        )
    return ToolError(
        code=ToolErrorCode.UNAVAILABLE,
        message="Simulated backend outage injected by the fault plan; no external call was made.",
        retryable=True,
        simulated=True,
    )
