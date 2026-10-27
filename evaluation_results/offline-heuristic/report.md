# Evaluation report: offline-heuristic

Generated 2026-10-27T05:32:03+00:00 - model: none (offline heuristic) - 10 cases x 2 approaches. Metric definitions: `docs/EVALUATION.md`.

**These are OFFLINE results produced with a rule-based stand-in for the language model** (`app/evaluation/heuristic_model.py`: 'the most recent change is the cause, else the most frequent error'). Both approaches use the same rules, so these numbers test the harness and show how the metrics respond. They say **nothing** about the quality of real LLM reasoning or about which architecture is better.

## Aggregate comparison

| Metric | multi_agent | single_agent |
|---|---|---|
| Strict accuracy | 40% | 40% |
| Lenient accuracy (acceptable inconclusive counts) | 60% | 60% |
| Wrong-conclusion rate (confidently wrong) | 0% | 0% |
| Failure rate | 0% | 0% |
| Top-1 hypothesis hit (gold cases) | 56% | 56% |
| Top-3 hypothesis hit (gold cases) | 89% | 89% |
| Distractor ranked first (gold cases) | 44% | 44% |
| Must-consider coverage (mean) | 50% | 50% |
| Key-evidence recall (mean) | 44% | 44% |
| Refs cited (total) | 50 | 50 |
| Ref existence rate | 100% | 100% |
| Ref availability rate | 100% | 100% |
| Lexical support rate (heuristic) | 100% | 100% |
| Tool calls per case (mean) | 4.9 | 4.9 |
| Model calls per case (mean) | 5.9 | 2 |
| Invalid model outputs (total) | 0 | 0 |
| Latency s (mean / median / max) | 0.01 / 0.011 / 0.019 | 0.005 / 0.005 / 0.008 |
| Tokens in / out (total) | n/a (not reported) | n/a (not reported) |

Outcome counts:

- **multi_agent**: correct 3, correct_inconclusive 1, acceptable_inconclusive 2, missed_inconclusive 4
- **single_agent**: correct 3, correct_inconclusive 1, acceptable_inconclusive 2, missed_inconclusive 4

## Per-case results

| Case | Scenario | Expected | Approach | Outcome | Conclusion | Top hypothesis (confidence) | Refs ok/cited | Tools | Model calls | Latency s |
|---|---|---|---|---|---|---|---|---|---|---|
| EVAL-01 | config_change_precedes_incident | root_cause | multi_agent | **correct** | probable_cause_identified | Reduce database connection pool size (payments-api config_ch... (medium) | 7/7 | 5 | 6 | 0.019 |
| EVAL-01 | config_change_precedes_incident | root_cause | single_agent | **correct** | probable_cause_identified | Reduce database connection pool size (payments-api config_ch... (medium) | 7/7 | 5 | 2 | 0.008 |
| EVAL-02 | misleading_deployment_correlation | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Update order confirmation email template (orders-service dep... (low) | 8/8 | 5 | 6 | 0.011 |
| EVAL-02 | misleading_deployment_correlation | root_cause | single_agent | **missed_inconclusive** | inconclusive | Update order confirmation email template (orders-service dep... (low) | 8/8 | 5 | 2 | 0.005 |
| EVAL-03 | conflicting_logs_and_metrics, config_change_precedes_incident | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Roll out batch reservations to all traffic (inventory-servic... (medium) | 2/2 | 5 | 6 | 0.011 |
| EVAL-03 | conflicting_logs_and_metrics, config_change_precedes_incident | root_cause | single_agent | **missed_inconclusive** | inconclusive | Roll out batch reservations to all traffic (inventory-servic... (medium) | 2/2 | 5 | 2 | 0.005 |
| EVAL-04 | missing_or_delayed_metrics, dependency_failure | root_cause | multi_agent | **acceptable_inconclusive** | inconclusive | Template engine upgrade (notification-service deployment) (medium) | 4/4 | 5 | 6 | 0.011 |
| EVAL-04 | missing_or_delayed_metrics, dependency_failure | root_cause | single_agent | **acceptable_inconclusive** | inconclusive | Template engine upgrade (notification-service deployment) (medium) | 4/4 | 5 | 2 | 0.005 |
| EVAL-05 | dependency_failure | root_cause | multi_agent | **missed_inconclusive** | inconclusive | New product carousel (web-frontend deployment) (medium) | 4/4 | 4 | 6 | 0.008 |
| EVAL-05 | dependency_failure | root_cause | single_agent | **missed_inconclusive** | inconclusive | New product carousel (web-frontend deployment) (medium) | 4/4 | 4 | 2 | 0.004 |
| EVAL-06 | retry_or_timeout_problem, config_change_precedes_incident | root_cause | multi_agent | **correct** | probable_cause_identified | Retry inventory calls up to 5 times without backoff (orders-... (medium) | 4/4 | 5 | 6 | 0.011 |
| EVAL-06 | retry_or_timeout_problem, config_change_precedes_incident | root_cause | single_agent | **correct** | probable_cause_identified | Retry inventory calls up to 5 times without backoff (orders-... (medium) | 4/4 | 5 | 2 | 0.005 |
| EVAL-07 | simulated_tool_failure, config_change_precedes_incident | root_cause | multi_agent | **acceptable_inconclusive** | inconclusive | Lower cache memory limit (redis-cache config_change) (medium) | 3/3 | 6 | 6 | 0.006 |
| EVAL-07 | simulated_tool_failure, config_change_precedes_incident | root_cause | single_agent | **acceptable_inconclusive** | inconclusive | Lower cache memory limit (redis-cache config_change) (medium) | 3/3 | 6 | 2 | 0.003 |
| EVAL-08 | multiple_plausible_hypotheses, missing_or_delayed_metrics | root_cause | multi_agent | **correct** | probable_cause_identified | Reduce database connection pool size (cost optimisation) (pa... (medium) | 6/6 | 5 | 6 | 0.011 |
| EVAL-08 | multiple_plausible_hypotheses, missing_or_delayed_metrics | root_cause | single_agent | **correct** | probable_cause_identified | Reduce database connection pool size (cost optimisation) (pa... (medium) | 6/6 | 5 | 2 | 0.005 |
| EVAL-09 | insufficient_evidence | inconclusive | multi_agent | **correct_inconclusive** | inconclusive | - | 0/0 | 4 | 5 | 0.007 |
| EVAL-09 | insufficient_evidence | inconclusive | single_agent | **correct_inconclusive** | inconclusive | - | 0/0 | 4 | 2 | 0.004 |
| EVAL-10 | strong_hypothesis_contradicted, misleading_deployment_correlation | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Upgrade ORM library (payments-api deployment) (low) | 12/12 | 5 | 6 | 0.01 |
| EVAL-10 | strong_hypothesis_contradicted, misleading_deployment_correlation | root_cause | single_agent | **missed_inconclusive** | inconclusive | Upgrade ORM library (payments-api deployment) (low) | 12/12 | 5 | 2 | 0.004 |

## Gold labels (for review)

- **EVAL-01**: A config change cut payments-api's DB pool from 40 to 8, exhausting connections. Inconclusive acceptable: False. Pool-size change 5 minutes before onset, a log confirming it applied, pool at 100% and DB CPU flat. The evidence is direct and consistent, so an inconclusive result is a miss.
- **EVAL-02**: postgres-orders ran out of disk space; writes failed before the deploy. Inconclusive acceptable: False. The report blames the deploy, but disk errors and the error-rate rise start 17 minutes before it, and the deploy only changed an email template.
- **EVAL-03**: The batch-reservations flag rollout caused lock contention on stock_reservations. Inconclusive acceptable: False. Inventory's own error rate and health checks look fine (409s count as client errors), but orders logs, DB lock waits and the flag rollout 10 minutes earlier agree.
- **EVAL-04**: The external fraud-check service slowed down (vendor-side); its metrics are missing. Inconclusive acceptable: True. Logs point to fraud-check timeouts and local resources are normal, but fraud-check metrics are missing, so the vendor-side cause is probable rather than proven. Inconclusive is acceptable.
- **EVAL-05**: The external email-provider is failing with HTTP 503. Inconclusive acceptable: False. Direct 503 errors from the provider from the moment success rate drops; no relevant change.
- **EVAL-06**: orders-service v6.1.0 retries without backoff, multiplying load on inventory-service. Inconclusive acceptable: False. Inventory request rate rose 5x while orders' own traffic stayed flat, starting right after the retry change.
- **EVAL-07**: redis-cache maxmemory was cut to 2gb; writes fail with OOM. Inconclusive acceptable: True. Metrics are unavailable (simulated tool failure), but the change record and OOM logs are direct. A correct answer is expected; inconclusive is acceptable because a source failed.
- **EVAL-08**: Pool reduction (CHG-E08-2) exhausted DB connections. Inconclusive acceptable: True. Two changes minutes apart and two failure signatures. Pool saturation is better supported, but fraud-check metrics are missing. Either cause, or inconclusive, is acceptable; both should be considered.
- **EVAL-09**: none (inconclusive expected) Inconclusive acceptable: True. No anomaly in metrics, no errors, no changes, and frontend logs are missing for the window. Any named root cause is unsupported; the correct outcome is inconclusive.
- **EVAL-10**: The billing-batch job's 200 workers exhausted postgres-payments connections. Inconclusive acceptable: False. The deploy looks guilty, but errors persist after the rollback, and DB connections jump when billing-batch starts 200 workers. Direct evidence contradicts the deploy hypothesis.
