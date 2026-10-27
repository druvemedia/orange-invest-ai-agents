# Evaluation report: live

Generated 2026-10-27T09:38:05+00:00 - model: gpt-4o-mini - 10 cases x 2 approaches. Metric definitions: `docs/EVALUATION.md`.

These results come from real model calls. They cover a small synthetic dataset (one run per case per approach, no repetitions), so treat differences of one or two cases as noise.

## Aggregate comparison

| Metric | multi_agent | single_agent |
|---|---|---|
| Runs completed / planned (not run) | 10 / 10 (0) | 10 / 10 (0) |
| Strict accuracy | 30% (3/10) | 20% (2/10) |
| Lenient accuracy (acceptable inconclusive counts) | 50% (5/10) | 40% (4/10) |
| Wrong-conclusion rate (confidently wrong) | 0% (0/10) | 10% (1/10) |
| Inconclusive outcomes | 80% (8/10) | 80% (8/10) |
| Failure rate | 0% (0/10) | 0% (0/10) |
| Top-1 hypothesis hit (gold cases) | 78% (7/9) | 33% (3/9) |
| Top-3 hypothesis hit (gold cases) | 78% (7/9) | 44% (4/9) |
| Distractor ranked first (gold cases) | 11% | 11% |
| Must-consider coverage (mean) | 100% | 100% |
| Key-evidence recall (mean) | 78% | 17% |
| Refs cited (total) | 242 | 117 |
| Ref existence rate | 100% | 100% |
| Ref availability rate | 100% | 100% |
| Lexical support rate (heuristic) | 79% | 70% |
| Unsupported supporting citations (unverifiable / assessed) | 0 / 82 | 0 / 29 |
| Evidence gaps (failed, missing or partial results) | 9 | 5 |
| Timeouts (tool, model or deadline) | 2 | 0 |
| Tool calls per case (mean) | 8.1 | 8.6 |
| Model calls per case (mean) | 8.6 | 3.5 |
| Invalid model outputs (total) | 9 | 1 |
| Latency s (mean / median / max) | 105.085 / 86.328 / 192.36 | 26.345 / 25.645 / 43.74 |
| Tokens in / out (total; runs measured) | 465643 / 51258 (10/10) | 162862 / 15798 (10/10) |
| Estimated cost USD (total; runs priced) | 0.1006 (10/10) | 0.0339 (10/10) |

## Configuration

- Model: llm_max_input_tokens 64000, llm_max_output_tokens 4000, llm_timeout_seconds 120.0, agent_max_critic_rounds 2, agent_max_evidence_requests 3, investigation_deadline_seconds 300.0
- Pricing: $0.15 / $0.6 per million input / output tokens (https://developers.openai.com/api/docs/models/gpt-4o-mini, checked 2026-10-27)
- Budget: $3.00; estimated spend $0.1345; worst case per run multi_agent $0.1920, single_agent $0.1440
- Limits: multi-agent 2 critic rounds, 2 attempts per task, deadline 300 s; single agent 6 steps, 2 attempts per step, 14 tool calls, deadline 300 s

Outcome counts:

- **multi_agent**: correct 2, correct_inconclusive 1, acceptable_inconclusive 2, missed_inconclusive 5
- **single_agent**: correct 1, correct_inconclusive 1, acceptable_inconclusive 2, missed_inconclusive 5, wrong_conclusion 1

## Per-case results

| Case | Scenario | Expected | Approach | Outcome | Conclusion | Top hypothesis (confidence) | Refs ok/cited | Tools | Model calls | Latency s |
|---|---|---|---|---|---|---|---|---|---|---|
| EVAL-01 | config_change_precedes_incident | root_cause | multi_agent | **correct** | probable_cause_identified | Database connection pool size reduced from 40 to 8 by CHG-E0... (high) | 27/27 | 8 | 9 | 190.316 |
| EVAL-01 | config_change_precedes_incident | root_cause | single_agent | **missed_inconclusive** | inconclusive | Database Connection Pool Size Reduction Caused Increased Lat... (medium) | 20/20 | 6 | 2 | 19.329 |
| EVAL-02 | misleading_deployment_correlation | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Insufficient Disk Space on 'postgres-orders' (medium) | 19/19 | 8 | 9 | 63.377 |
| EVAL-02 | misleading_deployment_correlation | root_cause | single_agent | **missed_inconclusive** | inconclusive | Insufficient Disk Space on Database Server (medium) | 7/7 | 5 | 2 | 19.507 |
| EVAL-03 | conflicting_logs_and_metrics, config_change_precedes_incident | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Batch Reservation Increase by CHG-E03-1 (Feature Flag Change... (medium) | 22/22 | 8 | 9 | 76.454 |
| EVAL-03 | conflicting_logs_and_metrics, config_change_precedes_incident | root_cause | single_agent | **wrong_conclusion** | probable_cause_identified | Transient inventory errors due to system load or reservation... (medium) | 8/8 | 12 | 5 | 27.597 |
| EVAL-04 | missing_or_delayed_metrics, dependency_failure | root_cause | multi_agent | **acceptable_inconclusive** | inconclusive | Latency due to fraud-check vendor response (medium) | 22/22 | 8 | 8 | 79.392 |
| EVAL-04 | missing_or_delayed_metrics, dependency_failure | root_cause | single_agent | **acceptable_inconclusive** | inconclusive | Third-party dependency latency caused payments timeouts (low) | 8/8 | 6 | 3 | 22.442 |
| EVAL-05 | dependency_failure | root_cause | multi_agent | **missed_inconclusive** | inconclusive | HTTP 503 Error from Email Provider (medium) | 16/16 | 7 | 8 | 51.909 |
| EVAL-05 | dependency_failure | root_cause | single_agent | **missed_inconclusive** | inconclusive | No emails were sent due to unknown service issue (low) | 11/11 | 12 | 4 | 23.809 |
| EVAL-06 | retry_or_timeout_problem, config_change_precedes_incident | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Change in orders-service deployment parameters by CHG-E06-1 (medium) | 21/21 | 8 | 9 | 192.36 |
| EVAL-06 | retry_or_timeout_problem, config_change_precedes_incident | root_cause | single_agent | **missed_inconclusive** | inconclusive | Increased Error Rates Following Deployment. (medium) | 11/11 | 8 | 3 | 27.481 |
| EVAL-07 | simulated_tool_failure, config_change_precedes_incident | root_cause | multi_agent | **acceptable_inconclusive** | inconclusive | Lower cache memory limit by CHG-E07-1 (medium) | 20/20 | 11 | 9 | 80.494 |
| EVAL-07 | simulated_tool_failure, config_change_precedes_incident | root_cause | single_agent | **acceptable_inconclusive** | inconclusive | Session Store or Cache Issues (low) | 5/5 | 16 | 6 | 43.74 |
| EVAL-08 | multiple_plausible_hypotheses, missing_or_delayed_metrics | root_cause | multi_agent | **correct** | probable_cause_identified | Reduced database connection pool size by CHG-E08-2 (high) | 26/26 | 8 | 8 | 98.694 |
| EVAL-08 | multiple_plausible_hypotheses, missing_or_delayed_metrics | root_cause | single_agent | **correct** | probable_cause_identified | Reducing the database connection pool size caused the issues... (medium) | 16/16 | 6 | 3 | 28.177 |
| EVAL-09 | insufficient_evidence | inconclusive | multi_agent | **correct_inconclusive** | inconclusive | Increased API Gateway Latency (low) | 31/31 | 7 | 8 | 125.693 |
| EVAL-09 | insufficient_evidence | inconclusive | single_agent | **correct_inconclusive** | inconclusive | Transient Latency Spike in API Gateway (low) | 11/11 | 9 | 5 | 34.05 |
| EVAL-10 | strong_hypothesis_contradicted, misleading_deployment_correlation | root_cause | multi_agent | **missed_inconclusive** | inconclusive | Upgrade ORM library by CHG-E10-1 caused DB connection issues (medium) | 38/38 | 8 | 9 | 92.163 |
| EVAL-10 | strong_hypothesis_contradicted, misleading_deployment_correlation | root_cause | single_agent | **missed_inconclusive** | inconclusive | Recent deployment caused issues (low) | 20/20 | 6 | 2 | 17.32 |

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
