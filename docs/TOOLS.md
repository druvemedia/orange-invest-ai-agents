# Evidence tools (Phase 1)

Five deterministic tools read local fixture data from `data/fixtures/` and return
**evidence, never conclusions**. None of them calls a network service or an LLM.

```python
from app.tools import build_evidence_tools

tools = build_evidence_tools()          # {tool name: tool instance}
result = tools["search_logs"].run({
    "start": "2026-10-20T13:30:00Z",
    "end": "2026-10-20T15:00:00Z",
    "services": ["payments-api"],
    "min_level": "ERROR",
})
result.status          # "ok" | "no_matches" | "partial" | "failed"
result.records         # list[LogRecord]
result.model_dump(mode="json")
```

`run()` accepts a dict (e.g. LLM tool-call arguments) or a query model instance.
It does not raise for expected problems. Bad input, injected faults and missing or
malformed data all come back as a result with `errors` and/or `warnings`.

## Tool contracts

Every list filter treats `[]` as "no filter". Timestamps must be timezone-aware
ISO 8601 (e.g. `2026-10-20T14:00:00Z`); time windows include both ends. Service
names are case-insensitive. Unknown query fields are rejected.

| Tool name | Query model | Record model | Filters | Order | Default / max `limit` |
|---|---|---|---|---|---|
| `search_logs` | `LogSearchQuery` | `LogRecord` | **`start`, `end`**, `services`, `min_level`, `keywords` (any, case-insensitive substring), `trace_id` | oldest first | 100 / 500 |
| `query_metrics` | `MetricQuery` | `MetricRecord` | **`start`, `end`**, `services`, `metric_names` | service, metric, then time | 500 / 2000 |
| `get_change_history` | `ChangeHistoryQuery` | `ChangeRecord` | **`start`, `end`**, `services`, `change_types` (`deployment`, `config_change`, `feature_flag`, `rollback`) | oldest first | 100 / 500 |
| `get_service_dependencies` | `DependencyQuery` | `DependencyRecord` | **`service`**, `direction` (`downstream`/`upstream`/`both`), `depth` 1–3 | service, then dependency | 100 / 500 |
| `search_historical_incidents` | `HistoricalIncidentQuery` | `HistoricalIncidentRecord` | `keywords`, `services`, `severities`, `occurred_after`, `occurred_before` | most keywords matched, then most recent | 10 / 50 |

**Bold** fields are required. Query models live in `app/schemas/queries.py` and records in
`app/schemas/evidence.py`. Each tool class has `name`, `description`, `query_model` and
`record_model`, so `query_model.model_json_schema()` can serve as a function-calling schema.

## Result envelope: `EvidenceResult[Record]`

| Field | Meaning |
|---|---|
| `source` | `logs`, `metrics`, `changes`, `dependencies` or `historical_incidents` |
| `query` | The validated query, with defaults filled in. For an invalid query, the raw input. |
| `records` | Matching records |
| `completeness` | `complete`: all relevant available data was searched and returned. `partial`: some relevant data was missing, unreadable or truncated. `none`: the tool failed. |
| `truncated` | More records matched than `limit` (always `partial`) |
| `warnings` | Human-readable caveats: coverage gaps, skipped lines, truncation, the absence note |
| `errors` | `ToolError` list (`code`, `message`, `retryable`, `simulated`); non-empty only when the tool failed |
| `record_count`, `status` | Derived: `failed` > `partial` > `no_matches` > `ok` |

Invariants enforced by the model: a failed result has at least one error and no
records, and a `complete` result has no errors and is not truncated.

## Imperfect evidence

| Situation | How it is represented |
|---|---|
| No matching records | `status="no_matches"`, `completeness="complete"`, plus a warning that absence of records does not prove absence of the event |
| Known missing data | `data/fixtures/coverage_gaps.jsonl` declares windows where a source has no data for a service. A query overlapping a gap is `partial` with the gap's reason in `warnings`. A query without a service filter covers all services, so it inherits every gap. |
| Malformed fixture lines | Each JSONL line is validated separately. Bad lines are skipped, the result is `partial`, and the warning gives the count and the first line number and field error (never the raw value). |
| Unknown service (dependencies) | A warning that its dependencies are unknown, not necessarily absent |
| Too many matches | `truncated=true`, `partial` |
| Invalid query | `failed`, `code="invalid_query"`, `retryable=false` |
| Dataset missing or unreadable | `failed`, `code="data_unavailable"`, `simulated=false` |
| Simulated timeout or outage | `failed`, `code="timeout"`/`"unavailable"`, `retryable=true`, **`simulated=true`** |

### Simulating failures

```python
from app.tools import FaultInjector, SimulatedFault, build_evidence_tools

tools = build_evidence_tools(faults=FaultInjector({"query_metrics": SimulatedFault.TIMEOUT}))
tools["query_metrics"].run({...}).errors[0].simulated   # True
```

Faults are fixed per tool name, so every call to that tool fails and other tools are unaffected.
Nothing sleeps, and the message states the failure was injected.

### Using other data

`FixtureStore(path)` points tools at a different directory containing the same file names,
for example a test's `tmp_path` or an evaluation scenario.
