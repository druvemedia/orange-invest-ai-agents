# Data

Synthetic fixture data shared by the evidence tools, the multi-agent system and the
single-agent baseline. Everything is invented. Do not store secrets or real customer
data here.

## `fixtures/`

One JSON object per line (JSONL). Each line must validate against the model listed below.
`tests/test_fixtures.py` fails if any line does not.

| File | Model | Contents |
|---|---|---|
| `incidents.jsonl` | `IncidentReport` | 3 current incident reports (inputs to investigation). They describe symptoms only. |
| `logs.jsonl` | `LogRecord` | 37 application log lines |
| `metrics.jsonl` | `MetricRecord` | 234 metric samples at 5-minute intervals |
| `changes.jsonl` | `ChangeRecord` | 7 deployments, config changes and feature-flag changes |
| `dependencies.jsonl` | `DependencyRecord` | 13 service dependency edges |
| `historical_incidents.jsonl` | `HistoricalIncidentRecord` | 6 past incidents with their recorded postmortem outcome |
| `coverage_gaps.jsonl` | `CoverageGap` | 3 known windows where a source has no data |

### Scenarios

Each current incident has a window of surrounding evidence. The data is deliberately
**ambiguous**. It contains several plausible explanations, confounders and gaps. No
fixture states the root cause of a current incident, and none should be added here. If
expected answers are needed for evaluation, keep them in a separate file that the tools
never read.

| Incident | Window (UTC) | Evidence included |
|---|---|---|
| `INC-2041` checkout payments slow/failing | 2026-10-20 13:30–15:00 | A deployment and a config change on `payments-api` minutes before onset. Latency, error-rate and DB-pool metrics degrade while traffic and DB CPU stay flat. Logs show both DB-pool timeouts and fraud-check timeouts. Fraud-check metrics and change history are missing (coverage gaps). Similar past incidents point in different directions. |
| `INC-2042` orders "item unavailable" | 2026-10-22 08:30–10:00 | A feature-flag rollout on `inventory-service` shortly before onset, plus an `orders-service` deploy the previous day that changed retry behaviour. Orders error rate rises while inventory's own error rate stays flat and its health checks pass (conflicting signals). Lock-wait and deadlock evidence on the inventory DB. |
| `INC-2043` "slow page loads" | 2026-10-24 17:30–19:00 | An incomplete report with no services or severity. A frontend deploy about 2 hours earlier, a traffic increase, and cache memory and eviction growth. `web-frontend` logs are missing for 17:45–18:30 (coverage gap). |

Outside these windows (e.g. 2026-10-10) there is no data, which is useful for testing empty results.
