"""Investigation plans: a validated, acyclic graph of task specs."""

from collections.abc import Collection, Sequence

from pydantic import Field, model_validator

from app.orchestration.models import TaskSpec
from app.schemas.common import StrictModel


def validate_task_graph(
    tasks: Sequence[TaskSpec], existing: Collection[str] = (), existing_finalizers: Collection[str] = ()
) -> None:
    """Raise ``ValueError`` for duplicate IDs, unknown dependencies, dependencies on finalizers or cycles.

    ``existing`` holds IDs of tasks already in a running investigation. New tasks may depend
    on them, but existing tasks never depend on new ones, so cycles can only form among ``tasks``.
    """
    ids: set[str] = set()
    for task in tasks:
        if task.task_id in ids or task.task_id in existing:
            raise ValueError(f"duplicate task_id '{task.task_id}'")
        ids.add(task.task_id)

    finalizers = {t.task_id for t in tasks if t.finalizer} | set(existing_finalizers)
    for task in tasks:
        unknown = [d for d in task.depends_on if d not in ids and d not in existing]
        if unknown:
            raise ValueError(f"task '{task.task_id}' depends on unknown task(s): {unknown}")
        # A finalizer waits for every other task, so depending on one would deadlock.
        on_finalizer = [d for d in task.depends_on if d in finalizers]
        if on_finalizer:
            raise ValueError(f"task '{task.task_id}' cannot depend on finalizer task(s): {on_finalizer}")

    cycle = _find_cycle({t.task_id: [d for d in t.depends_on if d in ids] for t in tasks})
    if cycle:
        raise ValueError(f"dependency cycle: {' -> '.join(cycle)}")


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in done:
            return None
        if node in visiting:
            return visiting[visiting.index(node) :] + [node]
        visiting.append(node)
        for dep in graph[node]:
            if cycle := visit(dep):
                return cycle
        visiting.pop()
        done.add(node)
        return None

    for node in graph:
        if cycle := visit(node):
            return cycle
    return None


class InvestigationPlan(StrictModel):
    tasks: list[TaskSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_graph(self) -> "InvestigationPlan":
        validate_task_graph(self.tasks)
        return self
