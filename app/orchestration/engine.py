"""Dependency-aware, bounded execution of investigation plans on asyncio.

Lifecycle of one ``execute()`` call:

1. The investigation moves PENDING -> RUNNING.
2. The scheduler repeatedly resolves pending tasks: it blocks those whose
   required dependencies failed, skips them once the attempt budget is spent,
   and starts ready ones (in plan order, up to ``max_concurrency``).
3. Each started task runs in its own asyncio task: attempts with a per-attempt
   timeout, retrying only retryable failures, up to the task's ``max_attempts``
   and the investigation's ``max_total_attempts``.
4. When nothing is running and nothing can start, the investigation finishes
   with a status and termination reason. A deadline or cancellation stops
   running tasks (CANCELLED) and unstarted ones (SKIPPED) first, so every task
   always ends in a terminal state.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from uuid import uuid4

from app.core.redaction import redact
from app.orchestration.handlers import TaskContext, TaskFailure, TaskHandler
from app.orchestration.models import (
    AttemptOutcome,
    DependencyPolicy,
    EngineLimits,
    EventKind,
    Investigation,
    InvestigationStatus,
    TaskAttempt,
    TaskError,
    TaskErrorCode,
    TaskOutput,
    TaskRecord,
    TaskStatus,
    TerminationReason,
    utcnow,
)
from app.orchestration.plan import InvestigationPlan, validate_task_graph
from app.schemas.evidence import EvidenceResult
from app.schemas.incident import IncidentReport

logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE = 300


class PlanRejectedError(ValueError):
    """The plan is valid on its own but cannot run on this engine (too large, unknown task kind)."""


class InvestigationEngine:
    def __init__(self, handlers: Mapping[str, TaskHandler], limits: EngineLimits | None = None) -> None:
        self._handlers = dict(handlers)
        self.limits = limits or EngineLimits()

    def create(
        self, incident: IncidentReport, plan: InvestigationPlan, investigation_id: str | None = None
    ) -> Investigation:
        if len(plan.tasks) > self.limits.max_tasks:
            raise PlanRejectedError(f"plan has {len(plan.tasks)} tasks; the limit is {self.limits.max_tasks}")
        self._check_kinds(plan.tasks)
        return Investigation(
            investigation_id=investigation_id or f"inv-{uuid4().hex[:12]}",
            incident=incident,
            limits=self.limits,
            tasks={spec.task_id: TaskRecord(spec=spec) for spec in plan.tasks},
        )

    async def execute(self, investigation: Investigation) -> Investigation:
        """Run a PENDING investigation to a terminal state. An investigation can only be executed once."""
        return await _Run(self._handlers, investigation).execute()

    async def run(
        self, incident: IncidentReport, plan: InvestigationPlan, investigation_id: str | None = None
    ) -> Investigation:
        return await self.execute(self.create(incident, plan, investigation_id))

    def _check_kinds(self, specs) -> None:
        unknown = sorted({s.kind for s in specs} - self._handlers.keys())
        if unknown:
            raise PlanRejectedError(f"no handler registered for task kind(s): {unknown}")


class _Run:
    """Mutable bookkeeping for a single execution. All state changes happen on the event loop thread."""

    def __init__(self, handlers: Mapping[str, TaskHandler], investigation: Investigation) -> None:
        self.handlers = handlers
        self.inv = investigation
        self.limits = investigation.limits
        self.running: dict[str, asyncio.Task[None]] = {}
        self.limit_reason: TerminationReason | None = None
        self.stop_error: TaskError | None = None

    # --- Top level ----------------------------------------------------------------------

    async def execute(self) -> Investigation:
        self.inv.start()
        try:
            async with asyncio.timeout(self.limits.deadline_seconds):
                await self._schedule()
        except TimeoutError:
            self.limit_reason = TerminationReason.DEADLINE_EXCEEDED
            await self._stop(
                TaskError(
                    code=TaskErrorCode.DEADLINE_EXCEEDED,
                    message=f"Investigation deadline of {self.limits.deadline_seconds}s was exceeded.",
                )
            )
        except asyncio.CancelledError:
            await self._stop(TaskError(code=TaskErrorCode.CANCELLED, message="The investigation was cancelled."))
            self.inv.finish(InvestigationStatus.CANCELLED, TerminationReason.CANCELLED)
            raise
        self.inv.finish(*self._outcome())
        logger.info(
            "investigation=%s status=%s reason=%s attempts=%d",
            self.inv.investigation_id,
            self.inv.status,
            self.inv.termination_reason,
            self.inv.attempts_used,
        )
        return self.inv

    async def _schedule(self) -> None:
        while True:
            self._advance_pending()
            if not self.running:
                return
            done, _ = await asyncio.wait(self.running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task_id in [tid for tid, t in self.running.items() if t in done]:
                # Runners record their own outcome; result() re-raises only on an engine bug.
                self.running.pop(task_id).result()

    def _advance_pending(self) -> None:
        changed = True
        while changed:
            changed = False
            for record in list(self.inv.tasks.values()):
                if record.status is TaskStatus.PENDING and self._resolve(record):
                    changed = True

    def _resolve(self, record: TaskRecord) -> bool:
        """Block, skip or start one pending task if possible. Returns True if its state changed."""
        spec = record.spec
        deps = [self.inv.tasks[d] for d in spec.depends_on]
        unsuccessful = [d.task_id for d in deps if d.status.is_terminal and d.status is not TaskStatus.SUCCEEDED]

        if spec.dependency_policy is DependencyPolicy.REQUIRE_SUCCESS and unsuccessful:
            record.block(
                unsuccessful,
                TaskError(
                    code=TaskErrorCode.DEPENDENCY_FAILED,
                    message=f"Required dependencies did not succeed: {', '.join(unsuccessful)}",
                ),
            )
            self.inv.log(EventKind.TASK_BLOCKED, record.task_id, f"blocked by {unsuccessful}")
            return True
        if not all(d.status.is_terminal for d in deps):
            return False
        if spec.finalizer and not all(t.status.is_terminal for t in self.inv.tasks.values() if not t.spec.finalizer):
            return False
        if self.inv.attempts_used >= self.limits.max_total_attempts:
            self.limit_reason = self.limit_reason or TerminationReason.ATTEMPT_BUDGET_EXHAUSTED
            record.skip(
                TaskError(
                    code=TaskErrorCode.ATTEMPT_BUDGET_EXHAUSTED,
                    message=f"Attempt budget of {self.limits.max_total_attempts} was used up before this task could start.",
                )
            )
            self.inv.log(EventKind.TASK_SKIPPED, record.task_id, "attempt budget exhausted")
            return True
        if len(self.running) >= self.limits.max_concurrency:
            return False

        record.start()
        self.inv.log(EventKind.TASK_STARTED, record.task_id)
        self.running[record.task_id] = asyncio.create_task(self._run_task(record), name=f"task:{record.task_id}")
        return True

    async def _stop(self, error: TaskError) -> None:
        """Cancel running tasks and skip unstarted ones, so that every task ends in a terminal state."""
        self.stop_error = error
        for task in self.running.values():
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)
        self.running.clear()
        for record in self.inv.tasks.values():
            if record.status is TaskStatus.PENDING:
                record.skip(error)
                self.inv.log(EventKind.TASK_SKIPPED, record.task_id, error.code)

    def _outcome(self) -> tuple[InvestigationStatus, TerminationReason]:
        records = list(self.inv.tasks.values())
        any_succeeded = any(r.status is TaskStatus.SUCCEEDED for r in records)
        required_missing = any(r.spec.required and r.status is not TaskStatus.SUCCEEDED for r in records)

        if self.limit_reason is not None:
            status = InvestigationStatus.PARTIALLY_COMPLETED
            if not any_succeeded or required_missing:
                status = InvestigationStatus.FAILED
            return status, self.limit_reason
        if all(r.status is TaskStatus.SUCCEEDED for r in records):
            return InvestigationStatus.COMPLETED, TerminationReason.ALL_TASKS_SUCCEEDED
        if required_missing:
            return InvestigationStatus.FAILED, TerminationReason.REQUIRED_TASK_DID_NOT_SUCCEED
        if not any_succeeded:
            return InvestigationStatus.FAILED, TerminationReason.NO_TASK_SUCCEEDED
        return InvestigationStatus.PARTIALLY_COMPLETED, TerminationReason.SOME_TASKS_DID_NOT_SUCCEED

    # --- One task -------------------------------------------------------------------------

    async def _run_task(self, record: TaskRecord) -> None:
        spec = record.spec
        handler = self.handlers[spec.kind]
        try:
            for number in range(1, spec.retry.max_attempts + 1):
                if self.inv.attempts_used >= self.limits.max_total_attempts:
                    self.limit_reason = self.limit_reason or TerminationReason.ATTEMPT_BUDGET_EXHAUSTED
                    self._fail(
                        record,
                        TaskError(
                            code=TaskErrorCode.ATTEMPT_BUDGET_EXHAUSTED,
                            message=f"Attempt budget of {self.limits.max_total_attempts} was used up before attempt {number}.",
                        ),
                    )
                    return
                self.inv.attempts_used += 1

                output, error, evidence = await self._attempt(record, handler, number)
                if error is None:
                    record.succeed(output)
                    self.inv.log(EventKind.TASK_SUCCEEDED, record.task_id, f"attempt {number}")
                    self._add_follow_ups(record, output)
                    return

                final = not error.retryable or number == spec.retry.max_attempts
                self.inv.log(
                    EventKind.ATTEMPT_FAILED,
                    record.task_id,
                    f"attempt {number}: {error.code}" + ("" if final else " (will retry)"),
                )
                if final:
                    self._fail(record, error)
                    return
                if spec.retry.backoff_seconds:
                    await asyncio.sleep(spec.retry.backoff_seconds)
        except asyncio.CancelledError:
            error = self.stop_error or TaskError(code=TaskErrorCode.CANCELLED, message="The task was cancelled.")
            record.cancel(error)
            self.inv.log(EventKind.TASK_CANCELLED, record.task_id, error.code)
            raise

    async def _attempt(
        self, record: TaskRecord, handler: TaskHandler, number: int
    ) -> tuple[TaskOutput | None, TaskError | None, EvidenceResult | None]:
        """Run one handler attempt and record it. Returns (output, error, evidence); exactly one of output/error is set."""
        spec = record.spec
        ctx = TaskContext(
            investigation_id=self.inv.investigation_id,
            incident=self.inv.incident,
            task=spec,
            attempt=number,
            dependencies={d: self.inv.tasks[d].model_copy(deep=True) for d in spec.depends_on},
            all_tasks=(
                {tid: t.model_copy(deep=True) for tid, t in self.inv.tasks.items() if tid != spec.task_id}
                if spec.finalizer
                else {}
            ),
        )
        started_at, t0 = utcnow(), time.perf_counter()
        output: TaskOutput | None = None
        error: TaskError | None = None
        evidence: EvidenceResult | None = None
        outcome = AttemptOutcome.FAILED

        timeout = asyncio.timeout(spec.timeout_seconds)
        try:
            async with timeout:
                result = await handler(ctx)
        except TaskFailure as failure:
            error, evidence = failure.error, failure.evidence
        except TimeoutError:
            outcome = AttemptOutcome.TIMED_OUT
            message = (
                f"Attempt exceeded the task timeout of {spec.timeout_seconds}s."
                if timeout.expired()
                else "The handler raised TimeoutError."
            )
            error = TaskError(code=TaskErrorCode.TIMEOUT, message=message, retryable=True)
        except asyncio.CancelledError:
            self._record(record, number, started_at, t0, AttemptOutcome.CANCELLED,
                         self.stop_error or TaskError(code=TaskErrorCode.CANCELLED, message="Attempt was cancelled."))
            raise
        except Exception as exc:  # A handler bug: never retried, never allowed to crash the engine.
            error = TaskError(code=TaskErrorCode.HANDLER_ERROR, message=_describe(exc))
        else:
            if not isinstance(result, TaskOutput):
                error = TaskError(
                    code=TaskErrorCode.HANDLER_ERROR,
                    message=f"Handler returned {type(result).__name__}, expected TaskOutput.",
                )
            else:
                error = self._check_follow_ups(result)
                if error is None:
                    output, evidence, outcome = result, result.evidence, AttemptOutcome.SUCCEEDED

        self._record(record, number, started_at, t0, outcome, error, evidence)
        return output, error, evidence

    def _record(self, record, number, started_at, t0, outcome, error, evidence=None) -> None:
        record.record_attempt(
            TaskAttempt(
                number=number,
                started_at=started_at,
                finished_at=utcnow(),
                duration_ms=(time.perf_counter() - t0) * 1000,
                outcome=outcome,
                error=error,
                evidence=evidence,
            )
        )

    def _fail(self, record: TaskRecord, error: TaskError) -> None:
        record.fail(error)
        self.inv.log(EventKind.TASK_FAILED, record.task_id, error.code)

    # --- Follow-up tasks --------------------------------------------------------------------

    def _check_follow_ups(self, output: TaskOutput) -> TaskError | None:
        new = output.follow_up_tasks
        if not new:
            return None
        try:
            validate_task_graph(
                new,
                existing=self.inv.tasks.keys(),
                existing_finalizers=[tid for tid, t in self.inv.tasks.items() if t.spec.finalizer],
            )
            unknown = sorted({s.kind for s in new} - self.handlers.keys())
            if unknown:
                raise ValueError(f"no handler registered for task kind(s): {unknown}")
        except ValueError as exc:
            return TaskError(code=TaskErrorCode.INVALID_FOLLOW_UP, message=_describe(exc))
        return None

    def _add_follow_ups(self, record: TaskRecord, output: TaskOutput) -> None:
        new = output.follow_up_tasks
        if not new:
            return
        if len(self.inv.tasks) + len(new) > self.limits.max_tasks:
            self.limit_reason = self.limit_reason or TerminationReason.TASK_BUDGET_EXHAUSTED
            self.inv.log(
                EventKind.FOLLOW_UPS_REJECTED,
                record.task_id,
                f"{len(new)} follow-up task(s) would exceed max_tasks={self.limits.max_tasks}",
            )
            return
        for spec in new:
            self.inv.tasks[spec.task_id] = TaskRecord(spec=spec, added_by=record.task_id)
        self.inv.log(EventKind.FOLLOW_UPS_ADDED, record.task_id, ", ".join(s.task_id for s in new))


def _describe(exc: BaseException) -> str:
    # Exception text can carry request details or credentials (e.g. from an SDK); redact before storing.
    text = redact(f"{type(exc).__name__}: {exc}")
    return text if len(text) <= _MAX_ERROR_MESSAGE else text[: _MAX_ERROR_MESSAGE - 3] + "..."
