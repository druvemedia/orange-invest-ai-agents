"""Typed state for investigations run by the orchestration engine.

Task specs are immutable. Task and investigation state is mutable, but only
through the transition methods below, which enforce the allowed transitions.
The model validators check the same invariants whenever state is constructed
from data, so an inconsistent record cannot be built either way.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.schemas.common import NonEmptyStr, StrictModel
from app.schemas.evidence import EvidenceResult, EvidenceStatus
from app.schemas.incident import IncidentReport

TaskId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")]

# Task kind handled by app.orchestration.handlers.EvidenceTaskHandler.
EVIDENCE_TASK_KIND = "evidence"


def utcnow() -> datetime:
    return datetime.now(UTC)


class InvalidTransitionError(RuntimeError):
    """A state change that the lifecycle does not allow (e.g. re-running a finished task)."""


# --- Task specification (immutable) -------------------------------------------------


class DependencyPolicy(StrEnum):
    # Run only if every dependency succeeded; otherwise the task is BLOCKED.
    REQUIRE_SUCCESS = "require_success"
    # Run once every dependency has finished, whatever the outcome. The handler sees the failures.
    ALLOW_FAILURES = "allow_failures"


class RetryPolicy(StrictModel):
    # Hard upper bound of 5 attempts per task; the engine also enforces a global attempt budget.
    max_attempts: int = Field(default=3, ge=1, le=5)
    backoff_seconds: float = Field(default=0.0, ge=0, le=10)


class TaskSpec(StrictModel):
    task_id: TaskId
    kind: NonEmptyStr
    description: str | None = None
    depends_on: list[TaskId] = Field(default_factory=list)
    # Static inputs for the handler. Results of dependencies are passed separately via TaskContext.
    inputs: dict[str, Any] = Field(default_factory=dict)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    dependency_policy: DependencyPolicy = DependencyPolicy.REQUIRE_SUCCESS
    # If a required task does not succeed, the investigation is FAILED rather than partially complete.
    required: bool = False

    @field_validator("depends_on")
    @classmethod
    def _unique_dependencies(cls, deps: list[str]) -> list[str]:
        if len(set(deps)) != len(deps):
            raise ValueError("depends_on contains duplicates")
        return deps

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "TaskSpec":
        if self.task_id in self.depends_on:
            raise ValueError(f"task '{self.task_id}' cannot depend on itself")
        return self


# --- Task results ----------------------------------------------------------------------


class TaskErrorCode(StrEnum):
    # Reported by evidence tools (values match app.schemas.evidence.ToolErrorCode).
    INVALID_QUERY = "invalid_query"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DATA_UNAVAILABLE = "data_unavailable"
    # Reported by handlers or the engine.
    INVALID_INPUT = "invalid_input"
    HANDLER_ERROR = "handler_error"
    INVALID_FOLLOW_UP = "invalid_follow_up"
    DEPENDENCY_FAILED = "dependency_failed"
    ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"


class TaskError(StrictModel):
    code: TaskErrorCode
    message: NonEmptyStr
    retryable: bool = False
    # True when the failure was injected for testing rather than caused by a real fault.
    simulated: bool = False


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TaskAttempt(StrictModel):
    number: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    duration_ms: float = Field(ge=0)
    outcome: AttemptOutcome
    error: TaskError | None = None
    # Evidence returned by this attempt, kept even when it failed, for provenance.
    evidence: SerializeAsAny[EvidenceResult] | None = None

    @model_validator(mode="after")
    def _error_matches_outcome(self) -> "TaskAttempt":
        if (self.outcome is AttemptOutcome.SUCCEEDED) != (self.error is None):
            raise ValueError("an attempt has an error if and only if it did not succeed")
        return self


class TaskOutput(StrictModel):
    """What a handler returns on success."""

    data: dict[str, Any] = Field(default_factory=dict)
    evidence: SerializeAsAny[EvidenceResult] | None = None
    # New tasks to add to the investigation (e.g. a critic asking for more evidence).
    follow_up_tasks: list[TaskSpec] = Field(default_factory=list)


# --- Task state (mutable through transitions) --------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Not run because a dependency did not succeed (REQUIRE_SUCCESS policy).
    BLOCKED = "blocked"
    # Not run because the investigation stopped first (budget, deadline or cancellation).
    SKIPPED = "skipped"
    # Was running when the investigation stopped.
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in (TaskStatus.PENDING, TaskStatus.RUNNING)


ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.SKIPPED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    **{status: frozenset() for status in TaskStatus if status.is_terminal},
}


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRecord(StateModel):
    spec: TaskSpec
    status: TaskStatus = TaskStatus.PENDING
    attempts: list[TaskAttempt] = Field(default_factory=list)
    output: TaskOutput | None = None
    # Final error for every non-successful terminal state.
    error: TaskError | None = None
    blocked_by: list[TaskId] = Field(default_factory=list)
    # Task that requested this one as a follow-up, if any.
    added_by: TaskId | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def task_id(self) -> str:
        return self.spec.task_id

    @model_validator(mode="after")
    def _check_invariants(self) -> "TaskRecord":
        if len(self.attempts) > self.spec.retry.max_attempts:
            raise ValueError("more attempts than the retry policy allows")
        if self.status is TaskStatus.SUCCEEDED:
            if self.output is None or self.error is not None:
                raise ValueError("a succeeded task has an output and no error")
        elif self.output is not None:
            raise ValueError("only a succeeded task has an output")
        if self.status.is_terminal and self.status is not TaskStatus.SUCCEEDED and self.error is None:
            raise ValueError(f"a {self.status} task must record why")
        if not self.status.is_terminal and self.error is not None:
            raise ValueError("an unfinished task cannot have a final error")
        if (self.status is TaskStatus.BLOCKED) != bool(self.blocked_by):
            raise ValueError("blocked_by is set if and only if the task is blocked")
        if self.status in (TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.SKIPPED) and self.attempts:
            raise ValueError(f"a {self.status} task cannot have attempts")
        return self

    def _move(self, new: TaskStatus) -> None:
        if new not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransitionError(f"task '{self.task_id}': {self.status} -> {new} is not allowed")
        self.status = new

    def start(self) -> None:
        self._move(TaskStatus.RUNNING)
        self.started_at = utcnow()

    def record_attempt(self, attempt: TaskAttempt) -> None:
        if self.status is not TaskStatus.RUNNING:
            raise InvalidTransitionError(f"task '{self.task_id}' is not running")
        if attempt.number != len(self.attempts) + 1 or attempt.number > self.spec.retry.max_attempts:
            raise InvalidTransitionError(f"task '{self.task_id}': unexpected attempt number {attempt.number}")
        self.attempts.append(attempt)

    def succeed(self, output: TaskOutput) -> None:
        self._move(TaskStatus.SUCCEEDED)
        self.output = output
        self.finished_at = utcnow()

    def fail(self, error: TaskError) -> None:
        self._finish_unsuccessfully(TaskStatus.FAILED, error)

    def cancel(self, error: TaskError) -> None:
        self._finish_unsuccessfully(TaskStatus.CANCELLED, error)

    def skip(self, error: TaskError) -> None:
        self._finish_unsuccessfully(TaskStatus.SKIPPED, error)

    def block(self, blocked_by: list[str], error: TaskError) -> None:
        if not blocked_by:
            raise InvalidTransitionError("block() needs at least one blocking dependency")
        self._finish_unsuccessfully(TaskStatus.BLOCKED, error)
        self.blocked_by = list(blocked_by)

    def _finish_unsuccessfully(self, status: TaskStatus, error: TaskError) -> None:
        self._move(status)
        self.error = error
        self.finished_at = utcnow()


# --- Investigation ------------------------------------------------------------------------


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in (InvestigationStatus.PENDING, InvestigationStatus.RUNNING)


class TerminationReason(StrEnum):
    ALL_TASKS_SUCCEEDED = "all_tasks_succeeded"
    SOME_TASKS_DID_NOT_SUCCEED = "some_tasks_did_not_succeed"
    REQUIRED_TASK_DID_NOT_SUCCEED = "required_task_did_not_succeed"
    NO_TASK_SUCCEEDED = "no_task_succeeded"
    ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"
    TASK_BUDGET_EXHAUSTED = "task_budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"


class EngineLimits(StrictModel):
    # Maximum tasks in an investigation, including follow-ups added while running.
    max_tasks: int = Field(default=25, ge=1, le=200)
    # Maximum handler attempts across all tasks (the step budget).
    max_total_attempts: int = Field(default=60, ge=1, le=500)
    max_concurrency: int = Field(default=5, ge=1, le=32)
    deadline_seconds: float = Field(default=120.0, gt=0, le=3600)


class EventKind(StrEnum):
    INVESTIGATION_STARTED = "investigation_started"
    TASK_STARTED = "task_started"
    ATTEMPT_FAILED = "attempt_failed"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    TASK_SKIPPED = "task_skipped"
    TASK_CANCELLED = "task_cancelled"
    FOLLOW_UPS_ADDED = "follow_ups_added"
    FOLLOW_UPS_REJECTED = "follow_ups_rejected"
    INVESTIGATION_FINISHED = "investigation_finished"


class InvestigationEvent(StrictModel):
    at: datetime
    kind: EventKind
    task_id: str | None = None
    detail: str = ""


class EvidenceOutcome(StrEnum):
    # Tool ran and returned complete evidence with records.
    COLLECTED = "collected"
    # Tool ran over complete data and nothing matched (not proof the event did not happen).
    NO_MATCHES = "no_matches"
    # Tool ran but some relevant data was missing, unreadable or truncated.
    PARTIAL = "partial"
    # Tool was called and failed; no evidence was obtained.
    FAILED = "failed"
    # Tool was never called successfully: blocked, skipped, cancelled or still pending.
    NOT_COLLECTED = "not_collected"


_EVIDENCE_OUTCOMES = {
    EvidenceStatus.OK: EvidenceOutcome.COLLECTED,
    EvidenceStatus.NO_MATCHES: EvidenceOutcome.NO_MATCHES,
    EvidenceStatus.PARTIAL: EvidenceOutcome.PARTIAL,
    EvidenceStatus.FAILED: EvidenceOutcome.FAILED,
}


class EvidenceRef(StrictModel):
    """One line of the evidence overview: what an evidence task did or did not obtain."""

    task_id: str
    tool: str | None
    outcome: EvidenceOutcome
    record_count: int
    attempts: int
    error_code: TaskErrorCode | None = None


class Investigation(StateModel):
    investigation_id: NonEmptyStr
    incident: IncidentReport
    limits: EngineLimits
    status: InvestigationStatus = InvestigationStatus.PENDING
    termination_reason: TerminationReason | None = None
    # Ordered by insertion: plan order, then follow-ups in the order they were added.
    tasks: dict[str, TaskRecord]
    events: list[InvestigationEvent] = Field(default_factory=list)
    attempts_used: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> "Investigation":
        for key, record in self.tasks.items():
            if key != record.task_id:
                raise ValueError(f"task key '{key}' does not match task_id '{record.task_id}'")
        if self.status.is_terminal:
            if self.termination_reason is None:
                raise ValueError("a finished investigation needs a termination reason")
            unfinished = [t.task_id for t in self.tasks.values() if not t.status.is_terminal]
            if unfinished:
                raise ValueError(f"a finished investigation cannot have unfinished tasks: {unfinished}")
        elif self.termination_reason is not None:
            raise ValueError("only a finished investigation has a termination reason")
        return self

    @computed_field
    @property
    def evidence(self) -> list[EvidenceRef]:
        return [_evidence_ref(t) for t in self.tasks.values() if t.spec.kind == EVIDENCE_TASK_KIND]

    def log(self, kind: EventKind, task_id: str | None = None, detail: str = "") -> None:
        self.events.append(InvestigationEvent(at=utcnow(), kind=kind, task_id=task_id, detail=detail))

    def start(self) -> None:
        if self.status is not InvestigationStatus.PENDING:
            raise InvalidTransitionError(f"investigation '{self.investigation_id}' has already been run")
        self.status = InvestigationStatus.RUNNING
        self.started_at = utcnow()
        self.log(EventKind.INVESTIGATION_STARTED)

    def finish(self, status: InvestigationStatus, reason: TerminationReason) -> None:
        if self.status is not InvestigationStatus.RUNNING or not status.is_terminal:
            raise InvalidTransitionError(f"investigation: {self.status} -> {status} is not allowed")
        unfinished = [t.task_id for t in self.tasks.values() if not t.status.is_terminal]
        if unfinished:
            raise InvalidTransitionError(f"cannot finish with unfinished tasks: {unfinished}")
        self.status = status
        self.termination_reason = reason
        self.finished_at = utcnow()
        self.log(EventKind.INVESTIGATION_FINISHED, detail=f"{status}: {reason}")


def _evidence_ref(task: TaskRecord) -> EvidenceRef:
    evidence = task.output.evidence if task.output else None
    if evidence is not None:
        outcome = _EVIDENCE_OUTCOMES[evidence.status]
    elif task.status is TaskStatus.FAILED:
        outcome = EvidenceOutcome.FAILED
    else:
        outcome = EvidenceOutcome.NOT_COLLECTED
    return EvidenceRef(
        task_id=task.task_id,
        tool=task.spec.inputs.get("tool"),
        outcome=outcome,
        record_count=evidence.record_count if evidence else 0,
        attempts=len(task.attempts),
        error_code=task.error.code if task.error else None,
    )
