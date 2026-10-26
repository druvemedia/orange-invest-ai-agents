"""Engine behaviour with deterministic fake handlers.

Concurrency is shown with asyncio synchronisation primitives (Barrier, Event), not
sleeps. The only real waits are task timeouts and deadlines of at most 0.2s, used
where the behaviour under test is a timeout.
"""

import asyncio
from collections import Counter

import pytest

from app.orchestration import (
    DependencyPolicy,
    EngineLimits,
    InvalidTransitionError,
    InvestigationEngine,
    InvestigationPlan,
    InvestigationStatus,
    PlanRejectedError,
    TaskContext,
    TaskError,
    TaskErrorCode,
    TaskFailure,
    TaskOutput,
    TaskSpec,
    TaskStatus,
    TerminationReason,
)
from app.orchestration.models import AttemptOutcome, EventKind
from app.schemas.incident import IncidentReport

INCIDENT = IncidentReport(incident_id="INC-T", summary="test incident")
SHORT = 0.05  # seconds; timeouts in tests that exercise a timeout


def task(task_id: str, *deps: str, script=("ok",), kind="scripted", **fields) -> TaskSpec:
    return TaskSpec(task_id=task_id, kind=kind, depends_on=list(deps), inputs={"script": list(script)}, **fields)


class Scripted:
    """Each attempt performs the next action in ``inputs.script`` (the last one repeats)."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.never = asyncio.Event()  # never set: awaiting it hangs until timeout or cancellation

    async def __call__(self, ctx: TaskContext) -> TaskOutput:
        self.calls[ctx.task.task_id] += 1
        script = ctx.task.inputs["script"]
        action = script[min(ctx.attempt, len(script)) - 1]
        match action:
            case "ok":
                return TaskOutput(data={"task": ctx.task.task_id, "attempt": ctx.attempt})
            case "retryable":
                raise TaskFailure(TaskError(code=TaskErrorCode.UNAVAILABLE, message="flaky", retryable=True))
            case "permanent":
                raise TaskFailure(TaskError(code=TaskErrorCode.INVALID_INPUT, message="bad input"))
            case "crash":
                raise RuntimeError("handler bug")
            case "bad_return":
                return {"not": "a TaskOutput"}  # type: ignore[return-value]
            case "hang":
                await self.never.wait()
        raise AssertionError(f"unknown action {action}")


def run(plan_tasks, handlers=None, **limits):
    scripted = Scripted()
    engine = InvestigationEngine(handlers or {"scripted": scripted}, EngineLimits(**limits))
    inv = asyncio.run(engine.run(INCIDENT, InvestigationPlan(tasks=plan_tasks), investigation_id="inv-test"))
    return inv, scripted


def statuses(inv) -> dict[str, TaskStatus]:
    return {tid: t.status for tid, t in inv.tasks.items()}


# --- Plan acceptance ----------------------------------------------------------------------


def test_engine_rejects_unknown_task_kind():
    engine = InvestigationEngine({"scripted": Scripted()})

    with pytest.raises(PlanRejectedError, match="no handler"):
        engine.create(INCIDENT, InvestigationPlan(tasks=[task("a", kind="mystery")]))


def test_engine_rejects_plans_over_task_budget():
    engine = InvestigationEngine({"scripted": Scripted()}, EngineLimits(max_tasks=2))

    with pytest.raises(PlanRejectedError, match="limit is 2"):
        engine.create(INCIDENT, InvestigationPlan(tasks=[task("a"), task("b"), task("c")]))


def test_investigation_executes_only_once():
    engine = InvestigationEngine({"scripted": Scripted()})
    inv = engine.create(INCIDENT, InvestigationPlan(tasks=[task("a")]))
    asyncio.run(engine.execute(inv))

    with pytest.raises(InvalidTransitionError):
        asyncio.run(engine.execute(inv))
    assert len(inv.tasks["a"].attempts) == 1


# --- Success, ordering, results ------------------------------------------------------------


def test_all_tasks_succeed_and_results_are_recorded():
    inv, scripted = run([task("a"), task("b"), task("c", "a", "b")])

    assert inv.status is InvestigationStatus.COMPLETED
    assert inv.termination_reason is TerminationReason.ALL_TASKS_SUCCEEDED
    assert {tid: t.output.data["task"] for tid, t in inv.tasks.items()} == {"a": "a", "b": "b", "c": "c"}
    assert scripted.calls == {"a": 1, "b": 1, "c": 1}
    assert inv.attempts_used == 3
    assert all(t.attempts[0].duration_ms >= 0 for t in inv.tasks.values())


def test_independent_tasks_run_concurrently():
    # Both handlers must be inside the barrier at the same time; run one after another, they would time out.
    barrier = asyncio.Barrier(2)

    async def meet(ctx: TaskContext) -> TaskOutput:
        await barrier.wait()
        return TaskOutput()

    inv, _ = run([task("a", kind="meet", timeout_seconds=1), task("b", kind="meet", timeout_seconds=1)], {"meet": meet})

    assert statuses(inv) == {"a": TaskStatus.SUCCEEDED, "b": TaskStatus.SUCCEEDED}


@pytest.mark.parametrize(("limit", "expected_peak"), [(1, 1), (2, 2), (5, 4)])
def test_concurrency_is_capped(limit, expected_peak):
    active = 0
    peak = 0

    async def track(ctx: TaskContext) -> TaskOutput:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        for _ in range(3):
            await asyncio.sleep(0)  # yield to the event loop so started tasks overlap
        active -= 1
        return TaskOutput()

    inv, _ = run([task(f"t{i}", kind="track") for i in range(4)], {"track": track}, max_concurrency=limit)

    assert inv.status is InvestigationStatus.COMPLETED
    assert peak == expected_peak


def test_dependent_task_waits_for_prerequisite_and_sees_its_result():
    gate = asyncio.Event()
    a_started = asyncio.Event()
    seen = {}

    async def slow_a(ctx: TaskContext) -> TaskOutput:
        a_started.set()
        await gate.wait()
        return TaskOutput(data={"value": 2})

    async def use_a(ctx: TaskContext) -> TaskOutput:
        dep = ctx.dependencies["a"]
        seen["status"] = dep.status
        return TaskOutput(data={"value": dep.output.data["value"] * 10})

    async def scenario():
        engine = InvestigationEngine({"slow": slow_a, "use": use_a, "scripted": Scripted()})
        inv = engine.create(
            INCIDENT, InvestigationPlan(tasks=[task("a", kind="slow"), task("b"), task("c", "a", kind="use")])
        )
        running = asyncio.create_task(engine.execute(inv))
        await a_started.wait()
        for _ in range(5):
            await asyncio.sleep(0)  # let anything that could start, start
        snapshot = statuses(inv)
        gate.set()
        await running
        return inv, snapshot

    inv, while_a_running = asyncio.run(scenario())

    assert while_a_running == {"a": TaskStatus.RUNNING, "b": TaskStatus.SUCCEEDED, "c": TaskStatus.PENDING}
    assert seen["status"] is TaskStatus.SUCCEEDED
    assert inv.tasks["c"].output.data == {"value": 20}
    assert inv.tasks["c"].started_at >= inv.tasks["a"].finished_at


def test_dependency_copies_cannot_modify_investigation_state():
    async def tamper(ctx: TaskContext) -> TaskOutput:
        ctx.dependencies["a"].status = TaskStatus.FAILED
        return TaskOutput()

    inv, _ = run([task("a"), task("b", "a", kind="tamper")], {"scripted": Scripted(), "tamper": tamper})

    assert inv.tasks["a"].status is TaskStatus.SUCCEEDED


# --- Failure isolation and dependency policies ----------------------------------------------


def test_failure_blocks_dependents_without_touching_unrelated_tasks():
    inv, scripted = run([task("a", script=["permanent"]), task("b"), task("c", "a"), task("d", "b"), task("e", "c")])

    assert statuses(inv) == {
        "a": TaskStatus.FAILED,
        "b": TaskStatus.SUCCEEDED,
        "c": TaskStatus.BLOCKED,
        "d": TaskStatus.SUCCEEDED,
        "e": TaskStatus.BLOCKED,
    }
    assert inv.tasks["c"].blocked_by == ["a"]
    assert inv.tasks["e"].blocked_by == ["c"]
    assert inv.tasks["c"].error.code is TaskErrorCode.DEPENDENCY_FAILED
    assert inv.tasks["b"].output.data == {"task": "b", "attempt": 1}
    assert "c" not in scripted.calls and "e" not in scripted.calls
    assert inv.status is InvestigationStatus.PARTIALLY_COMPLETED
    assert inv.termination_reason is TerminationReason.SOME_TASKS_DID_NOT_SUCCEED


def test_allow_failures_policy_runs_after_failed_dependency():
    seen = {}

    async def summarise(ctx: TaskContext) -> TaskOutput:
        seen.update({tid: rec.status for tid, rec in ctx.dependencies.items()})
        return TaskOutput()

    inv, _ = run(
        [task("a", script=["permanent"]), task("b"), task("s", "a", "b", kind="sum", dependency_policy=DependencyPolicy.ALLOW_FAILURES)],
        {"scripted": Scripted(), "sum": summarise},
    )

    assert inv.tasks["s"].status is TaskStatus.SUCCEEDED
    assert seen == {"a": TaskStatus.FAILED, "b": TaskStatus.SUCCEEDED}


def test_required_task_failure_fails_investigation():
    inv, _ = run([task("a"), task("b", script=["permanent"], required=True)])

    assert inv.status is InvestigationStatus.FAILED
    assert inv.termination_reason is TerminationReason.REQUIRED_TASK_DID_NOT_SUCCEED


def test_no_successful_task_fails_investigation():
    inv, _ = run([task("a", script=["permanent"]), task("b", "a")])

    assert inv.status is InvestigationStatus.FAILED
    assert inv.termination_reason is TerminationReason.NO_TASK_SUCCEEDED


# --- Retries ------------------------------------------------------------------------------------


def test_retryable_failure_is_retried_until_success():
    inv, scripted = run([task("a", script=["retryable", "retryable", "ok"], retry={"max_attempts": 3})])

    record = inv.tasks["a"]
    assert record.status is TaskStatus.SUCCEEDED
    assert [a.outcome for a in record.attempts] == [AttemptOutcome.FAILED, AttemptOutcome.FAILED, AttemptOutcome.SUCCEEDED]
    assert [a.number for a in record.attempts] == [1, 2, 3]
    assert record.attempts[0].error.code is TaskErrorCode.UNAVAILABLE
    assert scripted.calls["a"] == 3


def test_retries_stop_at_max_attempts_and_final_failure_is_recorded():
    inv, scripted = run([task("a", script=["retryable"], retry={"max_attempts": 4})])

    record = inv.tasks["a"]
    assert record.status is TaskStatus.FAILED
    assert len(record.attempts) == 4 == scripted.calls["a"]
    assert record.error.code is TaskErrorCode.UNAVAILABLE
    kinds = [e.kind for e in inv.events if e.task_id == "a"]
    assert kinds.count(EventKind.ATTEMPT_FAILED) == 4
    assert kinds[-1] is EventKind.TASK_FAILED


@pytest.mark.parametrize(
    ("action", "code"),
    [("permanent", TaskErrorCode.INVALID_INPUT), ("crash", TaskErrorCode.HANDLER_ERROR), ("bad_return", TaskErrorCode.HANDLER_ERROR)],
)
def test_non_retryable_failures_are_not_retried(action, code):
    inv, scripted = run([task("a", script=[action, "ok"], retry={"max_attempts": 5})])

    record = inv.tasks["a"]
    assert record.status is TaskStatus.FAILED
    assert scripted.calls["a"] == 1
    assert record.error.code is code
    assert record.error.retryable is False


def test_handler_crash_message_is_recorded_not_raised():
    inv, _ = run([task("a", script=["crash"])])

    assert inv.tasks["a"].error.message == "RuntimeError: handler bug"


# --- Timeouts, deadline, cancellation --------------------------------------------------------------


def test_timed_out_attempt_is_retried():
    inv, _ = run([task("a", script=["hang", "ok"], timeout_seconds=SHORT, retry={"max_attempts": 2})])

    record = inv.tasks["a"]
    assert record.status is TaskStatus.SUCCEEDED
    assert [a.outcome for a in record.attempts] == [AttemptOutcome.TIMED_OUT, AttemptOutcome.SUCCEEDED]
    assert record.attempts[0].error.code is TaskErrorCode.TIMEOUT
    assert record.attempts[0].error.simulated is False


def test_repeated_timeouts_fail_the_task():
    inv, _ = run([task("a", script=["hang"], timeout_seconds=SHORT, retry={"max_attempts": 2}), task("b")])

    assert inv.tasks["a"].status is TaskStatus.FAILED
    assert inv.tasks["a"].error.code is TaskErrorCode.TIMEOUT
    assert len(inv.tasks["a"].attempts) == 2
    assert inv.tasks["b"].status is TaskStatus.SUCCEEDED


def test_deadline_cancels_running_and_skips_unstarted_tasks():
    inv, _ = run(
        [task("fast"), task("stuck", script=["hang"], timeout_seconds=60), task("after", "stuck")],
        deadline_seconds=0.2,
    )

    assert statuses(inv) == {"fast": TaskStatus.SUCCEEDED, "stuck": TaskStatus.CANCELLED, "after": TaskStatus.SKIPPED}
    assert inv.tasks["stuck"].attempts[-1].outcome is AttemptOutcome.CANCELLED
    assert inv.tasks["stuck"].error.code is TaskErrorCode.DEADLINE_EXCEEDED
    assert inv.tasks["after"].error.code is TaskErrorCode.DEADLINE_EXCEEDED
    assert inv.status is InvestigationStatus.PARTIALLY_COMPLETED
    assert inv.termination_reason is TerminationReason.DEADLINE_EXCEEDED


def test_external_cancellation_leaves_consistent_final_state():
    started = asyncio.Event()
    scripted = Scripted()

    async def hang(ctx: TaskContext) -> TaskOutput:
        started.set()
        await asyncio.Event().wait()
        return TaskOutput()

    async def scenario():
        engine = InvestigationEngine({"hang": hang, "scripted": scripted})
        inv = engine.create(
            INCIDENT, InvestigationPlan(tasks=[task("h", kind="hang", timeout_seconds=60), task("next", "h")])
        )
        running = asyncio.create_task(engine.execute(inv))
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return inv

    inv = asyncio.run(scenario())

    assert inv.status is InvestigationStatus.CANCELLED
    assert inv.termination_reason is TerminationReason.CANCELLED
    assert statuses(inv) == {"h": TaskStatus.CANCELLED, "next": TaskStatus.SKIPPED}
    assert inv.tasks["h"].attempts[0].outcome is AttemptOutcome.CANCELLED
    assert "next" not in scripted.calls


# --- Budgets and runaway prevention ---------------------------------------------------------------


def test_attempt_budget_bounds_total_work():
    inv, scripted = run(
        [task("a", script=["retryable"]), task("b", script=["retryable"]), task("c")],
        max_concurrency=1,
        max_total_attempts=4,
    )

    assert inv.attempts_used == 4 == sum(scripted.calls.values())
    assert statuses(inv) == {"a": TaskStatus.FAILED, "b": TaskStatus.FAILED, "c": TaskStatus.SKIPPED}
    assert inv.tasks["a"].error.code is TaskErrorCode.UNAVAILABLE  # used all 3 of its own attempts
    assert inv.tasks["b"].error.code is TaskErrorCode.ATTEMPT_BUDGET_EXHAUSTED
    assert inv.tasks["c"].error.code is TaskErrorCode.ATTEMPT_BUDGET_EXHAUSTED
    assert inv.status is InvestigationStatus.FAILED
    assert inv.termination_reason is TerminationReason.ATTEMPT_BUDGET_EXHAUSTED


def test_follow_up_tasks_are_added_and_run_once():
    async def plan_more(ctx: TaskContext) -> TaskOutput:
        return TaskOutput(follow_up_tasks=[task("extra", ctx.task.task_id)])

    inv, scripted = run([task("seed", kind="more")], {"more": plan_more, "scripted": Scripted()})

    assert list(inv.tasks) == ["seed", "extra"]
    assert inv.tasks["extra"].status is TaskStatus.SUCCEEDED
    assert inv.tasks["extra"].added_by == "seed"
    assert inv.status is InvestigationStatus.COMPLETED


def test_endless_follow_ups_are_stopped_by_task_budget():
    # A critic that always asks for one more round must not loop forever.
    async def again(ctx: TaskContext) -> TaskOutput:
        n = int(ctx.task.task_id.split("-")[1]) + 1
        return TaskOutput(follow_up_tasks=[task(f"round-{n}", ctx.task.task_id, kind="again")])

    inv, _ = run([task("round-0", kind="again")], {"again": again}, max_tasks=5)

    assert len(inv.tasks) == 5
    assert all(len(t.attempts) == 1 for t in inv.tasks.values())
    assert inv.status is InvestigationStatus.PARTIALLY_COMPLETED
    assert inv.termination_reason is TerminationReason.TASK_BUDGET_EXHAUSTED
    assert any(e.kind is EventKind.FOLLOW_UPS_REJECTED for e in inv.events)


@pytest.mark.parametrize(
    "follow_up",
    [
        task("x", "nope"),  # unknown dependency
        task("seed"),  # duplicate ID
        task("x", kind="unregistered"),
    ],
)
def test_invalid_follow_ups_fail_the_producing_task(follow_up):
    async def bad(ctx: TaskContext) -> TaskOutput:
        return TaskOutput(follow_up_tasks=[follow_up])

    inv, _ = run([task("seed", kind="bad")], {"bad": bad, "scripted": Scripted()})

    assert list(inv.tasks) == ["seed"]
    assert inv.tasks["seed"].status is TaskStatus.FAILED
    assert inv.tasks["seed"].error.code is TaskErrorCode.INVALID_FOLLOW_UP


def test_every_task_ends_terminal_and_handlers_match_attempts():
    inv, scripted = run(
        [
            task("a", script=["retryable", "ok"]),
            task("b", script=["permanent"]),
            task("c", "a"),
            task("d", "b"),
            task("e", "c", "d", dependency_policy=DependencyPolicy.ALLOW_FAILURES),
        ]
    )

    assert all(t.status.is_terminal for t in inv.tasks.values())
    assert {tid: len(t.attempts) for tid, t in inv.tasks.items() if t.attempts} == dict(scripted.calls)
    assert inv.attempts_used == sum(scripted.calls.values())
