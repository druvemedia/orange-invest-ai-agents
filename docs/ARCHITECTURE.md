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
    engine.py        InvestigationEngine: scheduling, retries, timeouts, budgets, finalizers
  llm/               provider-neutral LLMClient, OpenAIClient, ScriptedLLMClient (tests)
  agents/            the six agents (Phase 3, described below)
    contracts.py     typed inputs/outputs of every agent
    evidence.py      evidence references and their validation (RefIndex)
    base.py          LLMAgent: call model, validate, check, fall back, store envelope
    orchestrator.py  scope + plan + budgets; investigate() entry point
    observability.py, dependency.py, reasoning.py, critic.py, reporting.py
  cli.py             python -m app.cli INC-2041  (live run; needs LLM settings)
  core/redaction.py  credential redaction for stored/logged error text
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
  The only model SDK is the official `openai` package, used only inside `app/llm/openai_client.py`.

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
   | model timeout / rate limit / unavailable / invalid output (agents) | `model_*` | yes |
   | model rejected the request (auth, 4xx) | `model_rejected` | no |
   | any other exception, or a return value that isn't a `TaskOutput` | `handler_error` (message redacted) | no (treated as a bug) |
   | invalid follow-up tasks | `invalid_follow_up` | no |

   Retries stop at the task's `max_attempts` or when the investigation's
   `max_total_attempts` is used up, whichever comes first. Every attempt is recorded.
4. A handler may return `follow_up_tasks`; the critic loop uses this. They are validated
   against the current graph and added with `added_by` set. If adding them would exceed
   `max_tasks`, they are rejected and logged, which stops runaway loops.
   A task with `finalizer=True` starts only after every non-finalizer task has finished,
   including follow-ups added later, and gets copies of all tasks in `TaskContext.all_tasks`.
   No task may depend on a finalizer. A finalizer still counts against budgets and is SKIPPED
   if the deadline or attempt budget runs out first.
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
- A timed-out or cancelled evidence tool's worker thread **still finishes in the background**;
  Phase 3 does not change this. It is harmless for the local fixtures. Agents use the
  SDK's async client, so cancelling an agent task does cancel its HTTP request.

## Agents (Phase 3)

Six agents, each a task handler on the engine. They communicate only through typed task
results in the shared investigation state; no agent calls another directly.

| Agent | Task(s) | Reads | Produces |
|---|---|---|---|
| **Orchestrator** | `orchestrate` | incident report | `InvestigationScope` (window, focus services, keywords), then the whole task graph as follow-ups; sets every budget |
| **Observability** | `observability` | `ev.logs`, `ev.metrics` | observations (`obs-N`, each `fact` or `interpretation`, cited), onset estimate, plus code-computed metric summaries and data gaps |
| **Dependency** | `dependency` | `ev.changes`, `ev.deps.<service>` | findings (`dep-N`, cited), plus code-computed change relations (minutes before report; relation to focus services through the dependency graph) |
| **Reasoning** | `reasoning.r1`, `reasoning.r2`, … | both agents' findings, all evidence, previous hypotheses, critique, new evidence | up to 5 competing hypotheses with supporting/contradicting refs, missing information, confidence |
| **Critic** | `critic.r1`, `critic.r2`, … | the hypotheses and everything Reasoning saw | issues, verdict, and validated evidence requests that become follow-up tasks |
| **Reporting** | `report` (finalizer) | the whole final state | `FinalReport`: conclusion or *inconclusive*, ranked hypotheses, facts vs assumptions, limitations, corrective actions |

The **evidence tools** run as `evidence` tasks (`ev.*`). The model never calls tools, shell or
code. It can only *request* a query through the Critic, and code validates that request against
the registered tool's query schema before scheduling it. Each request is judged on its own:
- An unknown tool, an unknown hypothesis, invalid JSON or a schema-invalid query is rejected with a
  recorded reason, as is a query already run (earlier or in the same response) or one over the
  per-round limit.
- Valid requests from the same response still run.
- The tool name's JSON schema carries an enum of the registered tools, so strict structured output
  already constrains it. The per-request check is the second line of defence.

### Data flow and feedback loop

```
orchestrate ─► ev.logs, ev.metrics ─────────► observability ─┐
            ├► ev.changes, ev.deps.<svc> ───► dependency ─────┼─► reasoning.r1 ─► critic.r1
            └► ev.history ────────────────────────────────────┘                    │
                                                                                    │ verdict revise/insufficient
              ┌─────────────────────────────────────────────────────────────────────┘ and rounds remain
              ▼
       ev.c1.1 … (validated requests) ─► reasoning.r2 (sees critique + new evidence) ─► critic.r2 ─► …

report (finalizer) ─ runs after everything above has finished, whatever the outcome
```

Example (fixtures, scripted model): `critic.r1` judged H1's "high" confidence unjustified while
fraud-check metrics were missing. It requested `search_logs` for "config reloaded", which became
`ev.c1.1`. `reasoning.r2` cited the new record `ev.c1.1#0` (the pool change took effect at 13:55:20)
and lowered H1 to medium. `critic.r2` accepted, and the report named H1 as the probable cause at
medium confidence, with H2 still open.

### Evidence references and grounding

References are `"<task>"` (a whole result; cite it for gaps), `"<task>#<n>"` (a tool record)
and `"<task>/<finding-id>"` (an agent finding, which itself cites records). After schema
validation, every agent's output is checked in code:

- every reference must be one the agent was shown; invented references are rejected (retryable);
- a failed or never-collected result can't be cited as support or contradiction, and an
  observation labelled `fact` can't rest on one;
- reasoning confidence is **capped by code**: no direct evidence → low; high needs direct
  support from ≥2 evidence sources and nothing contradicting it; insufficient evidence → all low;
- the Critic adds rule-based issues (no support, only interpretations, absence used as support)
  that the model can't overrule. An "accept" with rule issues becomes "revise";
- the report may name a probable cause only if `conclusion_allowed`: final reasoning not
  insufficient, the final critic verdict is accept, and the top hypothesis is at least medium
  and strictly ahead of the next. Otherwise the conclusion is forced to `inconclusive`. Report
  confidences can't exceed the reasoning's, and uncited "facts" are moved to assumptions.

Every adjustment code makes is recorded in the task's `adjustments`, so you can see what the
model said and what was accepted.

### Termination controls

| Control | Default | Where |
|---|---|---|
| Critic rounds | 2 (`AGENT_MAX_CRITIC_ROUNDS`) | Critic stops requesting once `round == max_rounds` |
| Evidence requests per round | 3 (`AGENT_MAX_EVIDENCE_REQUESTS`) | excess, invalid or repeated queries are rejected and recorded |
| Tasks per investigation | 40 | engine `max_tasks`; follow-ups beyond it are rejected |
| Model/tool attempts in total | 80 | engine `max_total_attempts` |
| Attempts per agent / evidence task | 2 / 2 | `RetryPolicy`; only transient errors are retried |
| Agent task timeout | `LLM_TIMEOUT_SECONDS` + 30 s | engine per-attempt timeout |
| Deadline | 300 s (`INVESTIGATION_DEADLINE_SECONDS`) | engine; running tasks cancelled, rest skipped |

A report always exists:
- If the reporting model fails on its last attempt, a deterministic, *inconclusive* report is built
  from the state.
- If the report task never ran (deadline or budget), `investigate()` builds the same fallback report.
- Orchestrator scoping falls back to a fixed window around the reported time.
- Observability, Dependency and Reasoning skip the model and say so when they have no evidence at all.

### Model provider

`app/llm/openai_client.py` calls `AsyncOpenAI().responses.parse(model, instructions, input,
text_format=<output model>, max_output_tokens)`, then validates the returned text against the
output model itself.
- SDK retries are off (`max_retries=0`), so the engine is the only, bounded place where retries happen.
- Provider exceptions are mapped to fixed messages (kind and HTTP status only). Their text, which can
  echo partly masked keys, is never stored, logged or chained.
- Engine-level `handler_error` messages pass through `app.core.redaction.redact`.
- The API key is read only from `LLM_API_KEY` (a `SecretStr`) in `build_llm_client`.
- The model name comes only from `LLM_MODEL`.

Agents depend only on the `LLMClient` protocol. Tests use `ScriptedLLMClient`, which validates
scripted responses exactly like real output.

The SDK validates the model's text against the output model inside `responses.parse`. A
`pydantic.ValidationError` raised there is mapped to a retryable `invalid_output`, the same as
our own validation. `OpenAIClient.calls` records each call's agent, outcome, latency and token
usage (no content), and the CLI prints these totals.

**Live verification status: verified once (2026-10-26, `gpt-4o-mini`, SDK 3.37.0).** One run of
`python -m app.cli INC-2041`:
- 9 model calls, all HTTP 200, with all six output schemas accepted as strict structured output.
- The investigation `completed` in 104 s and 17 attempts, using 55,052 input and 5,743 output tokens.
- One grounding retry: Reasoning cited `dep-3` instead of `dependency/dep-3`. The check rejected
  it and the retry passed.
- The critic loop ran 2 rounds and executed 3 valid evidence requests.
- The report was **inconclusive** with five low-confidence hypotheses. The final critic verdict was
  still "revise" when the round limit was reached.

This verifies the integration, not the reasoning quality; see README, "Live smoke test".
