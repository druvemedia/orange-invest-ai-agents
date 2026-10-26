# Architecture

```
app/
  factory.py         create_app(settings=None); importing it has no side effects
  main.py            ASGI entry point: app = create_app(), so bad config fails at startup
  core/              settings (pydantic-settings, SecretStr) and logging
  api/routes/        HTTP routers; one module per resource
  schemas/           Pydantic models: incident report, evidence records,
                     tool queries, EvidenceResult envelope
  tools/             five deterministic evidence tools, fixture loader,
                     fault injection (see docs/TOOLS.md)
  orchestration/     investigation engine (Phase 2, described below)
    models.py        task specs, task/investigation state, transitions, evidence overview
    plan.py          InvestigationPlan and task-graph validation
    handlers.py      handler interface, EvidenceTaskHandler, evidence_task()
    engine.py        InvestigationEngine: scheduling, retries, timeouts, budgets
  agents/            LLM agents                         (Phase 3)
  evaluation/        offline evaluation harness         (later phases)
tests/               pytest suite
data/fixtures/       synthetic JSONL evidence shared by all phases (see data/README.md)
docs/                design notes
```

## Conventions

- Configuration comes only from environment variables / `.env`, read through
  `app.core.config.get_settings()`. Routes receive settings via FastAPI `Depends`,
  so tests override them with `app.dependency_overrides`.
- Secrets are `SecretStr`: masked in `repr`/logs and never returned by any endpoint.
- Evidence tools return `EvidenceResult` and never raise for expected failures.
  They report what was observed and how complete it is, not what it means.
- No agent or orchestration framework is used; the engine is plain `asyncio` + Pydantic.

## Orchestration engine

The engine runs an **investigation plan**: a set of tasks with declared dependencies.
It knows nothing about LLMs. Each task has a `kind`, and the engine calls the
**handler** registered for that kind. Phase 1's evidence tools run through the
`evidence` handler, and the Phase 3 agents will be further handler kinds.

```python
import asyncio
from app.orchestration import (EVIDENCE_TASK_KIND, EvidenceTaskHandler, InvestigationEngine,
                               InvestigationPlan, evidence_task)
from app.schemas.incident import IncidentReport
from app.tools import build_evidence_tools

incident = IncidentReport(incident_id="INC-2041", summary="Checkout card payments failing")
window = {"start": "2026-10-20T13:30:00Z", "end": "2026-10-20T15:00:00Z"}
plan = InvestigationPlan(tasks=[
    evidence_task("logs", "search_logs", {**window, "services": ["payments-api"]}),
    evidence_task("changes", "get_change_history", {**window, "services": ["payments-api"]}),
    evidence_task("history", "search_historical_incidents",
                  {"services": ["payments-api"]}, depends_on=["changes"]),
])
engine = InvestigationEngine({EVIDENCE_TASK_KIND: EvidenceTaskHandler(build_evidence_tools())})
investigation = asyncio.run(engine.run(incident, plan))
investigation.status, investigation.termination_reason, investigation.evidence
```

### Building blocks

| Concept | Model | Notes |
|---|---|---|
| Task definition | `TaskSpec` (immutable) | `task_id`, `kind`, `depends_on`, `inputs`, `retry` (`max_attempts` 1–5, optional backoff), `timeout_seconds` per attempt, `dependency_policy`, `required` |
| Plan | `InvestigationPlan` | Rejects duplicate IDs, unknown dependencies, self-dependencies and cycles (the error names the cycle path) |
| Handler | `async (TaskContext) -> TaskOutput` | Context: incident, task spec, attempt number, **copies** of finished dependency records. Raises `TaskFailure(TaskError)` for a classified failure. |
| Task state | `TaskRecord` | Status, every `TaskAttempt` (timing, outcome, error, evidence), final output or error, `blocked_by`, `added_by` |
| Investigation | `Investigation` | Incident, limits, ordered tasks, event log, attempts used, status, termination reason, computed `evidence` overview |
| Limits | `EngineLimits` | `max_tasks` (25), `max_total_attempts` (60), `max_concurrency` (5), `deadline_seconds` (120) |

### Task lifecycle

```
PENDING ──► RUNNING ──► SUCCEEDED
   │           ├──────► FAILED       (non-retryable error, or retries/attempt budget used up)
   │           └──────► CANCELLED    (deadline exceeded or investigation cancelled mid-attempt)
   ├──────► BLOCKED                  (a dependency did not succeed; REQUIRE_SUCCESS policy)
   └──────► SKIPPED                  (never started: attempt budget, deadline or cancellation)
```

Every other transition raises `InvalidTransitionError`. Terminal states are final,
so a task can never run twice. Every non-successful terminal state carries a
`TaskError` that says why. The model validators enforce the same invariants when
state is built from data.

**Dependency policies.** `require_success` (default): the task runs only if every
dependency succeeded; otherwise it is BLOCKED as soon as one dependency fails.
`allow_failures`: the task runs once every dependency has finished, whatever the
outcome. It is meant for steps such as a summariser that must report what failed.

### Execution

1. `execute()` moves the investigation PENDING → RUNNING. It can only be executed once.
2. The scheduler resolves pending tasks in plan order. It blocks tasks whose
   required dependencies failed, skips tasks once the attempt budget is spent, and
   starts ready tasks as separate asyncio tasks, up to `max_concurrency`. Independent
   tasks therefore run concurrently. Evidence tools are synchronous and run via
   `asyncio.to_thread`, so they overlap too.
3. Each attempt runs under `asyncio.timeout(timeout_seconds)`. Errors are classified:

   | Failure | Code | Retried? |
   |---|---|---|
   | `TaskFailure` with `retryable=True` (e.g. tool `unavailable`/`timeout`) | as given | yes |
   | `TaskFailure` with `retryable=False` (e.g. `invalid_query`, `data_unavailable`, `invalid_input`) | as given | no |
   | attempt exceeded `timeout_seconds` | `timeout` | yes |
   | any other exception, or a return value that isn't a `TaskOutput` | `handler_error` | no (treated as a bug) |
   | invalid follow-up tasks | `invalid_follow_up` | no |

   Retries stop at the task's `max_attempts` or when the investigation's
   `max_total_attempts` is used up, whichever comes first. Every attempt is recorded.
4. A handler may return `follow_up_tasks`, which is the hook for the Phase 3 critic loop.
   They are validated against the current graph and added with `added_by` set. If adding
   them would exceed `max_tasks`, they are rejected and logged, which stops runaway loops.
5. When nothing is running and nothing can start, the investigation finishes.
   If the deadline passes, running tasks are cancelled (CANCELLED) and unstarted ones
   skipped (SKIPPED). External cancellation does the same, then re-raises
   `CancelledError`. Every task always ends in a terminal state.

### Final status

Checked in this order:

| Condition | Status | `termination_reason` |
|---|---|---|
| Cancelled from outside | `cancelled` | `cancelled` |
| Deadline, attempt budget or task budget hit | `partially_completed`, or `failed` if nothing succeeded or a `required` task did not | `deadline_exceeded` / `attempt_budget_exhausted` / `task_budget_exhausted` |
| Every task succeeded | `completed` | `all_tasks_succeeded` |
| A `required` task did not succeed | `failed` | `required_task_did_not_succeed` |
| No task succeeded | `failed` | `no_task_succeeded` |
| Otherwise | `partially_completed` | `some_tasks_did_not_succeed` |

### Evidence quality vs task success

Task status says whether a tool **ran**. Evidence quality is reported separately in
`investigation.evidence` (one `EvidenceRef` per evidence task):

| `outcome` | Meaning |
|---|---|
| `collected` | Tool ran over complete data and returned records |
| `no_matches` | Tool ran over complete data and found nothing. This is not proof the event did not happen. |
| `partial` | Tool ran but data was missing, unreadable or truncated (see the result's warnings) |
| `failed` | Tool was called and failed; no evidence was obtained |
| `not_collected` | Tool was never run successfully (blocked, skipped or cancelled) |

A failed or skipped task never gets an output, so the engine cannot invent evidence.
Failed attempts keep the tool's failed `EvidenceResult`, including the `simulated`
flag, for provenance. A successful task's output holds the full `EvidenceResult`,
with the exact query, records, completeness and warnings.

### Known limits

- The investigation state lives in memory. It serialises to JSON, but loading it
  back from JSON is not supported, because evidence records are not re-typed on load.
- A timed-out or cancelled evidence tool's worker thread finishes in the background.
  This is harmless for the local fixtures, but Phase 3 handlers doing real I/O should
  use async clients so cancellation takes effect.
