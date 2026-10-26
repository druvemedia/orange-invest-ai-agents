"""The handler interface, and the handler that runs the existing evidence tools.

A handler is any async callable ``(TaskContext) -> TaskOutput``. To report a
classified failure it raises ``TaskFailure``; the engine retries it only if the
error is ``retryable``. Any other exception is treated as a non-retryable bug.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.orchestration.models import (
    EVIDENCE_TASK_KIND,
    TaskError,
    TaskErrorCode,
    TaskOutput,
    TaskRecord,
    TaskSpec,
)
from app.schemas.evidence import EvidenceResult, EvidenceStatus
from app.schemas.incident import IncidentReport
from app.tools.base import EvidenceTool


@dataclass(frozen=True)
class TaskContext:
    investigation_id: str
    incident: IncidentReport
    task: TaskSpec
    attempt: int
    # Copies of the finished dependency records, keyed by task ID. Changing them has no effect.
    dependencies: Mapping[str, TaskRecord]
    # Finalizer tasks only: copies of every task in the investigation. Empty for other tasks.
    all_tasks: Mapping[str, TaskRecord] = field(default_factory=dict)


class TaskFailure(Exception):
    def __init__(self, error: TaskError, evidence: EvidenceResult | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.evidence = evidence


TaskHandler = Callable[[TaskContext], Awaitable[TaskOutput]]


class EvidenceTaskHandler:
    """Runs one evidence tool. Task inputs: ``{"tool": <tool name>, "query": {...}}``.

    Evidence that is partial or has no matches is a success; the task output keeps
    the full ``EvidenceResult`` so its completeness and warnings are not lost. A
    failed tool result becomes a ``TaskFailure`` that keeps the tool's own
    ``retryable`` and ``simulated`` flags.
    """

    def __init__(self, tools: Mapping[str, EvidenceTool]) -> None:
        self._tools = dict(tools)

    async def __call__(self, ctx: TaskContext) -> TaskOutput:
        tool_name = ctx.task.inputs.get("tool")
        query = ctx.task.inputs.get("query")
        tool = self._tools.get(tool_name) if isinstance(tool_name, str) else None
        if tool is None:
            raise TaskFailure(TaskError(code=TaskErrorCode.INVALID_INPUT, message=f"Unknown evidence tool: {tool_name!r}"))
        if not isinstance(query, Mapping):
            raise TaskFailure(TaskError(code=TaskErrorCode.INVALID_INPUT, message="inputs.query must be an object"))

        # Tools are synchronous; a thread keeps the event loop free so independent tasks overlap.
        result = await asyncio.to_thread(tool.run, query)

        if result.status is EvidenceStatus.FAILED:
            first = result.errors[0]
            raise TaskFailure(
                TaskError(
                    code=TaskErrorCode(first.code),
                    message=first.message,
                    retryable=all(e.retryable for e in result.errors),
                    simulated=first.simulated,
                ),
                evidence=result,
            )
        return TaskOutput(evidence=result)


def evidence_task(task_id: str, tool: str, query: Mapping[str, Any], **spec_fields: Any) -> TaskSpec:
    """Build a TaskSpec that runs ``tool`` with ``query``. Extra fields go to TaskSpec (retry, timeout, ...)."""
    return TaskSpec(
        task_id=task_id,
        kind=EVIDENCE_TASK_KIND,
        inputs={"tool": tool, "query": dict(query)},
        **spec_fields,
    )
