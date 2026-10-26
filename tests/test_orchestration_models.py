"""State models, transitions and plan validation (no execution)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.orchestration import (
    InvalidTransitionError,
    Investigation,
    InvestigationPlan,
    InvestigationStatus,
    TaskError,
    TaskErrorCode,
    TaskOutput,
    TaskRecord,
    TaskSpec,
    TaskStatus,
    TerminationReason,
    validate_task_graph,
)
from app.orchestration.models import AttemptOutcome, EngineLimits, TaskAttempt
from app.schemas.incident import IncidentReport

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
ERROR = TaskError(code=TaskErrorCode.UNAVAILABLE, message="down", retryable=True)


def spec(task_id: str, *deps: str, **fields) -> TaskSpec:
    return TaskSpec(task_id=task_id, kind="fake", depends_on=list(deps), **fields)


def attempt(number: int, outcome=AttemptOutcome.SUCCEEDED, error=None) -> TaskAttempt:
    return TaskAttempt(number=number, started_at=NOW, finished_at=NOW, duration_ms=0, outcome=outcome, error=error)


# --- TaskSpec ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"task_id": "Has Spaces"},
        {"task_id": ""},
        {"kind": " "},
        {"depends_on": ["a", "a"]},
        {"depends_on": ["t"]},  # self-dependency
        {"retry": {"max_attempts": 6}},
        {"retry": {"max_attempts": 0}},
        {"timeout_seconds": 0},
        {"unexpected": 1},
    ],
)
def test_task_spec_rejects_invalid_fields(fields):
    with pytest.raises(ValidationError):
        TaskSpec(**{"task_id": "t", "kind": "fake", **fields})


# --- Plan validation --------------------------------------------------------------------


def test_plan_accepts_a_dag():
    plan = InvestigationPlan(tasks=[spec("a"), spec("b"), spec("c", "a", "b")])

    assert [t.task_id for t in plan.tasks] == ["a", "b", "c"]


def test_plan_rejects_unknown_dependency():
    with pytest.raises(ValidationError, match=r"depends on unknown task\(s\): \['missing'\]"):
        InvestigationPlan(tasks=[spec("a", "missing")])


def test_plan_rejects_cycle_and_names_it():
    with pytest.raises(ValidationError, match="dependency cycle: a -> c -> b -> a"):
        InvestigationPlan(tasks=[spec("a", "c"), spec("b", "a"), spec("c", "b"), spec("d")])


def test_plan_rejects_duplicate_ids():
    with pytest.raises(ValidationError, match="duplicate task_id 'a'"):
        InvestigationPlan(tasks=[spec("a"), spec("a")])


def test_plan_rejects_empty():
    with pytest.raises(ValidationError):
        InvestigationPlan(tasks=[])


def test_graph_validation_allows_dependencies_on_existing_tasks():
    validate_task_graph([spec("new", "old")], existing={"old"})

    with pytest.raises(ValueError, match="duplicate"):
        validate_task_graph([spec("old")], existing={"old"})


# --- TaskRecord transitions ----------------------------------------------------------------


def test_task_lifecycle_success():
    record = TaskRecord(spec=spec("a"))
    record.start()
    record.record_attempt(attempt(1))
    record.succeed(TaskOutput(data={"x": 1}))

    assert record.status is TaskStatus.SUCCEEDED
    assert record.started_at and record.finished_at
    TaskRecord.model_validate(record.model_dump())  # still satisfies invariants


@pytest.mark.parametrize(
    "action",
    [
        lambda r: r.succeed(TaskOutput()),  # PENDING -> SUCCEEDED skips RUNNING
        lambda r: r.fail(ERROR),
        lambda r: r.cancel(ERROR),
        lambda r: r.record_attempt(attempt(1)),
    ],
)
def test_pending_task_rejects_invalid_transitions(action):
    with pytest.raises(InvalidTransitionError):
        action(TaskRecord(spec=spec("a")))


@pytest.mark.parametrize("finish", ["succeed", "fail", "cancel"])
def test_finished_task_cannot_run_again(finish):
    record = TaskRecord(spec=spec("a"))
    record.start()
    record.succeed(TaskOutput()) if finish == "succeed" else getattr(record, finish)(ERROR)

    with pytest.raises(InvalidTransitionError):
        record.start()


def test_attempts_are_bounded_by_retry_policy():
    record = TaskRecord(spec=spec("a", retry={"max_attempts": 2}))
    record.start()
    record.record_attempt(attempt(1, AttemptOutcome.FAILED, ERROR))
    record.record_attempt(attempt(2, AttemptOutcome.FAILED, ERROR))

    with pytest.raises(InvalidTransitionError):
        record.record_attempt(attempt(3, AttemptOutcome.FAILED, ERROR))


def test_attempt_numbers_must_be_sequential():
    record = TaskRecord(spec=spec("a"))
    record.start()

    with pytest.raises(InvalidTransitionError):
        record.record_attempt(attempt(2))


def test_block_requires_blocking_dependencies():
    with pytest.raises(InvalidTransitionError):
        TaskRecord(spec=spec("a")).block([], ERROR)


@pytest.mark.parametrize(
    "data",
    [
        {"status": "succeeded"},  # no output
        {"status": "failed"},  # no error
        {"status": "blocked", "error": ERROR.model_dump()},  # no blocked_by
        {"status": "pending", "error": ERROR.model_dump()},
        {"status": "pending", "output": {}},
        {"status": "skipped", "error": ERROR.model_dump(), "attempts": [attempt(1).model_dump()]},
    ],
)
def test_inconsistent_task_state_is_rejected(data):
    with pytest.raises(ValidationError):
        TaskRecord.model_validate({"spec": spec("a").model_dump(), **data})


def test_attempt_outcome_and_error_must_agree():
    with pytest.raises(ValidationError):
        attempt(1, AttemptOutcome.FAILED, error=None)
    with pytest.raises(ValidationError):
        attempt(1, AttemptOutcome.SUCCEEDED, error=ERROR)


# --- Investigation ------------------------------------------------------------------------


def _investigation(**fields) -> Investigation:
    return Investigation(
        investigation_id="inv-1",
        incident=IncidentReport(incident_id="INC-1", summary="s"),
        limits=EngineLimits(),
        tasks={"a": TaskRecord(spec=spec("a"))},
        **fields,
    )


def test_investigation_cannot_start_twice():
    inv = _investigation()
    inv.start()

    with pytest.raises(InvalidTransitionError, match="already been run"):
        inv.start()


def test_investigation_cannot_finish_with_unfinished_tasks():
    inv = _investigation()
    inv.start()

    with pytest.raises(InvalidTransitionError, match="unfinished"):
        inv.finish(InvestigationStatus.COMPLETED, TerminationReason.ALL_TASKS_SUCCEEDED)


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "completed"},  # no reason, unfinished task
        {"status": "running", "termination_reason": "cancelled"},
    ],
)
def test_inconsistent_investigation_state_is_rejected(fields):
    with pytest.raises(ValidationError):
        _investigation(**fields)


def test_task_keys_must_match_ids():
    with pytest.raises(ValidationError, match="does not match"):
        Investigation(
            investigation_id="inv-1",
            incident=IncidentReport(incident_id="INC-1", summary="s"),
            limits=EngineLimits(),
            tasks={"b": TaskRecord(spec=spec("a"))},
        )
