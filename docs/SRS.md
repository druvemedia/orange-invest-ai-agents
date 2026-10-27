# Software Requirements Specification: Multi-Agent Production Incident Investigation System

Status legend, used for every requirement:

| Status | Meaning |
|---|---|
| **Verified** | implemented and covered by passing automated tests (offline, no network) |
| **Partial** | implemented, but validated only partly (for example offline only, or by a few real-model runs) |
| **Not implemented** | not built, or built but never run; stated as a limitation |

Paths are relative to the repository root. Test names refer to functions in `tests/`.

## 1. Purpose and scope

The system investigates a production incident report and produces ranked, evidence-backed root-cause hypotheses,
or an explicit **inconclusive** result when the evidence does not justify a cause. It was built for the Orange
Invest AI Engineer assignment.

- **In scope:** six collaborating agents on a bounded orchestration engine; five evidence tools over local
  synthetic data; a single-agent baseline; an evaluation harness over 10 labelled synthetic incidents; a REST API,
  minimal web UI and CLI.
- **Out of scope:** real observability integrations, authentication, persistence, multi-process deployment.

## 2. System overview and actors

| Actor | Interaction |
|---|---|
| Engineer (user) | submits an incident and reads the report, through the web UI (`GET /`), the REST API or the CLI (`python -m app.cli`) |
| Evaluator | runs the tests and the offline evaluation (`python -m app.evaluation offline`) |
| Language model | OpenAI `gpt-4o-mini` through the Responses API with structured outputs, or the keyless offline stand-in `HeuristicLLMClient` (not an AI model) |
| Evidence tools | five deterministic tools over synthetic JSONL data (`app/tools/`) |

Architecture, flow and sequence diagrams are in [ARCHITECTURE.md](ARCHITECTURE.md#diagrams).

## 3. Functional requirements

| ID | Requirement | Implementation | Tests | Status |
|---|---|---|---|---|
| FR-01 | Accept an incident (summary, timezone-aware reported time, optional services, severity, context, evidence source); reject invalid input | `app/api/routes/investigations.py::submit`, `app/schemas/investigation_api.py::InvestigationRequest`, `app/cli.py` | `test_submission_returns_202_with_location_and_job`, `test_invalid_submissions_are_rejected_with_422` | Verified |
| FR-02 | Scope the investigation (time window, focus services, keywords), bounded and corrected in code | `app/agents/orchestrator.py::resolve_scope`, `OrchestratorAgent` | `tests/test_agents.py` | Verified |
| FR-03 | Collect evidence through five tools, with independent tasks running in parallel | `OrchestratorAgent.plan_tasks`, `app/orchestration/engine.py` | `test_independent_tasks_run_concurrently`, `tests/test_tools.py` | Verified |
| FR-04 | Compare metrics with a pre-incident baseline, fetching data from before onset; report an insufficient baseline rather than "no change" | `app/agents/evidence.py::RefIndex.metric_baseline`, `metric_query_start` | `test_eval01_pool_at_100_percent_is_a_change_from_the_38_percent_pre_incident_baseline`, `tests/test_metric_baseline.py` | Partial: verified offline; real runs 3–4 classified the pool correctly |
| FR-05 | Observability findings from logs and metrics, each cited | `app/agents/observability.py` | `tests/test_agents.py` | Verified |
| FR-06 | Dependency findings from change history and service topology, each cited | `app/agents/dependency.py` | `tests/test_agents.py` | Verified |
| FR-07 | Competing hypotheses, ranked, with supporting and contradicting evidence, confidence and missing information | `app/agents/reasoning.py`, `app/agents/contracts.py::Hypothesis` | `tests/test_agents.py` | Verified |
| FR-08 | Critic review with verdict, issues and validated follow-up evidence requests; at most `AGENT_MAX_CRITIC_ROUNDS` rounds | `app/agents/critic.py` (`next_round`) | `test_full_investigation_completes_with_critic_feedback_round`, `test_critic_requested_evidence_reaches_reasoning`, `test_critic_contract_offers_only_registered_tools` | Verified |
| FR-09 | Ground citations: references must exist, evidence classes, confidence caps, no "increased load" from latency alone when request rate is flat | `app/agents/evidence.py`, `app/agents/grounding.py`, `app/agents/reasoning.py::cap_confidence` | `tests/test_phase6_quality.py`, `test_latency_alone_does_not_support_increased_load_when_request_rate_is_flat` | Partial: verified offline; load rule active in one real run |
| FR-10 | Timeline and evidence-independence checks | `app/agents/temporal.py`, `app/agents/independence.py` | `tests/test_phase7_temporal_independence.py` | Verified; exercised in real runs 2–4 |
| FR-11 | Name a cause only when every decision check passes; otherwise inconclusive with the failing checks | `app/agents/adjudication.py`, `app/agents/report_rules.py::finalize_report` | `test_inconclusive_with_the_exact_blocking_reason`, `test_strong_direct_evidence_without_contradiction_is_accepted_despite_revise` | Verified; exercised in real runs 2–4 |
| FR-12 | Final report separates facts, interpretations and assumptions; lists limitations, actions and what would confirm or rule out the leader; the top hypothesis names the change it cites | `app/agents/report_rules.py`, `app/agents/reporting.py` | `test_facts_assumptions_and_interpretations_are_separated`, `test_top_hypothesis_resting_on_a_change_names_it`, `tests/test_report_wording.py` | Verified offline |
| FR-13 | Report view with every citation resolved to its record; JSON and Markdown | `app/product/report_view.py` | `test_report_resolves_every_citation_to_retrieved_evidence`, `tests/test_report_view.py` | Verified |
| FR-14 | Recover from tool and agent failures (retry retryable errors once, record gaps, fall back deterministically) | `app/orchestration/engine.py`, `app/agents/base.py::LLMAgent.__call__`, `app/tools/faults.py` | `test_tool_failure_and_partial_evidence_are_reported`, `test_invalid_model_output_exhausts_bounded_retries`, `test_injected_faults_return_failed_results` | Verified |
| FR-15 | Single-agent baseline with the same tools, budgets, rules and report contract | `app/baseline/single_agent.py` | `tests/test_baseline.py` | Verified offline |
| FR-16 | Evaluation harness: 10 labelled cases, both approaches, accuracy with denominators, grounding, unsupported citations, evidence gaps, timeouts, tool calls, latency, tokens, estimated cost, configuration | `app/evaluation/` | `tests/test_eval_runner.py`, `tests/test_eval_metrics.py`, `tests/test_eval_dataset.py`, `tests/test_live_evaluation.py` | Partial: offline mode run; live mode implemented but **never run** |
| FR-17 | Billed model calls only with an explicit opt-in | `app/llm/factory.py::check_llm_settings` | `test_without_opt_in_no_client_is_built`, `tests/test_billing_guard.py` | Verified |
| FR-18 | Refuse a model request whose input could exceed the per-request budget, before sending | `app/llm/openai_client.py::input_token_bound` | `test_an_oversized_request_is_refused_before_anything_is_sent`, `tests/test_input_budget.py` | Verified; no request refused in real runs 3–4 |
| FR-19 | Verify evidence against each hypothesis: named changes cited; mechanism-consistent failure logs not counted against it; both-ways citations dropped; metric direction checked against claimed increases; structured per-citation assessment (exists, class, sources, services, relevance, flags) | `app/agents/grounding.py::check_citations`, `assess_citations` | `tests/test_verification.py` (run 4's real evidence and counterexamples) | Partial: verified offline; checked on run 4's recorded evidence only; no new real-model run |
| FR-20 | Fair live evaluation: the same inputs, tools, model and scoring for both approaches; two opt-ins and a budget; preflight estimate; budget stop with `not_run` runs kept; unique output folders; README and checksums | `app/evaluation/live.py`, `runner.py`, `__main__.py` | `tests/test_live_evaluation.py`, `test_live_evaluation_needs_both_opt_ins_and_a_budget` | Partial: implemented and tested offline; **never run** |

## 4. Non-functional requirements

| ID | Requirement | Implementation | Tests | Status |
|---|---|---|---|---|
| NFR-01 | Bounded execution: ≤ 40 tasks, ≤ 80 attempts, ≤ 5 concurrent tasks, 300 s deadline, 360 s hard stop per job, ≤ 2 attempts per task | `app/orchestration/models.py::EngineLimits`, `app/agents/orchestrator.py::AgentConfig`, `app/product/service.py` | `test_concurrency_is_capped`, `test_endless_follow_ups_are_stopped_by_task_budget`, `test_hard_time_limit_fails_a_stuck_job` | Verified |
| NFR-02 | Bounded cost: ≤ 16 model requests per investigation with defaults; 64,000 input and 4,000 output tokens per request; SDK retries off | `app/llm/openai_client.py`, `AgentConfig` | `tests/test_input_budget.py` | Verified offline; real runs used 8–10 calls, about $0.01–0.02 each |
| NFR-03 | Secrets never exposed: `SecretStr`, redacted error text, `.env` git-ignored | `app/core/config.py`, `app/core/redaction.py` | `test_internal_errors_are_safe_and_secret_free`, `tests/test_llm.py` | Verified |
| NFR-04 | Termination: every task ends in a terminal state and a report is always produced, a deterministic fallback if needed | `app/orchestration/engine.py`, `OrchestratorAgent.investigate` | `test_engine_deadline_still_produces_a_report`, `test_finalizer_runs_after_all_tasks_including_follow_ups` | Verified |
| NFR-05 | Auditability: code adjustments, citation checks, decision checks and per-call usage recorded and shown | report `adjustments`, `Adjudication`, `CallStats` logging | `tests/test_report_view.py` | Verified |
| NFR-06 | No evaluation-answer leakage: labels read only by the scorer after every run; prompt templates and every prompt and input actually sent (all 10 cases, both approaches) checked against all labels | `app/evaluation/runner.py`, `app/evaluation/dataset.py` | `test_labels_are_loaded_only_after_every_investigation`, `test_no_model_facing_prompt_reveals_an_evaluation_answer`, `test_gold_labels_never_reach_the_investigation` | Verified |
| NFR-07 | Reproducible offline operation: tests and offline evaluation need no key or network | `HeuristicLLMClient`, `ScriptedLLMClient` | the full suite (626 tests) | Verified |
| NFR-08 | Authentication and authorisation | none | none | Not implemented: keep the server on 127.0.0.1 |
| NFR-09 | Persistence of jobs across restarts | in-memory only (last 50 jobs) | none | Not implemented |

## 5. Agent and tool interfaces

Agents exchange only typed results (`app/agents/contracts.py`) through the engine's shared state; no agent calls
another. Every model output is a strict structured output, validated, then checked in code.

| Agent | Task | Input | Output |
|---|---|---|---|
| Orchestrator | `orchestrate` | incident | `InvestigationScope` and the task plan |
| Observability | `observability` | `ev.logs`, `ev.metrics`, metric summaries | cited observations, onset estimate, data gaps |
| Dependency | `dependency` | `ev.changes`, `ev.deps.<service>` | cited change and topology findings |
| Reasoning | `reasoning.rN` | findings, all evidence, previous critique | up to 5 ranked `Hypothesis` objects |
| Critic | `critic.rN` | hypotheses and their evidence | verdict, issues, evidence requests |
| Reporting | `report` (finalizer) | the whole final state | `FinalReport` after code rules and adjudication |

| Tool | Purpose | Query model |
|---|---|---|
| `search_logs` | log records by window, service, level, keywords | `LogSearchQuery` |
| `query_metrics` | metric samples by window, service, metric | `MetricQuery` |
| `get_change_history` | deployments, config changes, flags, rollbacks | `ChangeHistoryQuery` |
| `get_service_dependencies` | dependency edges around a service | `DependencyQuery` |
| `search_historical_incidents` | past incidents by keywords and services | `HistoricalIncidentQuery` |

Every tool returns an `EvidenceResult` with records, completeness, warnings and typed errors, including simulated
failures from `app/tools/faults.py`. Contracts: [TOOLS.md](TOOLS.md).

## 6. Investigation lifecycle and output

The lifecycle is shown in [ARCHITECTURE.md](ARCHITECTURE.md#investigation-flow). In order:
1. Validation.
2. Job creation.
3. Scope.
4. Parallel evidence.
5. Observability and Dependency findings.
6. Reasoning, then the Critic, then bounded follow-up rounds.
7. Reporting.

**The report** (`GET /investigations/{id}/report`, or `.md`) contains:
- a conclusion, `probable_cause_identified` or `inconclusive`;
- ranked hypotheses with resolved supporting and contradicting citations and their evidence classes;
- facts, interpretations and assumptions;
- every decision check with pass/fail;
- timeline checks and evidence availability;
- actions, limitations and process metadata.

## 7. External dependencies and configuration

- **Runtime:** Python ≥ 3.12, plus `fastapi`, `uvicorn[standard]`, `pydantic-settings` and `openai` 3.37 or newer
  (`requirements.txt`).
- **Tests:** `pytest` and `httpx` (`requirements-dev.txt`).
- **No database or external service** is required.
- **Live mode** needs an OpenAI API key and network access.
- **Configuration** comes from environment variables or `.env`. The template is `.env.example`.

## 8. Error handling and failure scenarios

| Scenario | Behaviour |
|---|---|
| Invalid request or unknown evidence source | HTTP 422, nothing runs |
| No usable model, or billed provider without opt-in | HTTP 503 / CLI exit 2; no client created |
| Too many running jobs | HTTP 429 with `Retry-After` |
| Tool failure (real or simulated) | retried once if retryable; then recorded as a gap; agents continue |
| Invalid or failed model output | retried once if retryable; Orchestrator and Reporting fall back deterministically; other agents' tasks fail and downstream tasks continue |
| Request over the input budget | refused before sending, not retried |
| Deadline (300 s) or attempt budget | remaining tasks cancelled or skipped; deterministic inconclusive fallback report |
| Hard stop (360 s) | job marked failed |

## 9. Evaluation requirements and current results

- **Dataset:** 10 labelled synthetic incidents (`data/eval/`), including a misleading deployment, conflicting
  signals, missing metrics, a simulated tool failure, competing causes and insufficient evidence. Labels are
  stored separately, and the scorer reads them only after every run.
- **Offline results** (`evaluation_results/offline-heuristic/`): both approaches score strict 0.4, lenient 0.6 and
  wrong 0.0, using a rule-based stand-in shared by both. **These scores measure the harness, not model accuracy,
  and do not show either approach is better.**
- **Real-model results:** four `gpt-4o-mini` runs of EVAL-01
  ([LIVE_RUN_EVAL01_ANALYSIS.md](LIVE_RUN_EVAL01_ANALYSIS.md)).
  - **None scored correct** against the unchanged answer key: three were inconclusive, and one named only the
    mechanism.
  - Run 4 used a prompt example that revealed EVAL-01's answer (since removed), so it is not clean evidence.
- **Not done:** no real-model comparison of the multi-agent system with the single-agent baseline.

## 10. Constraints, assumptions, limitations and future work

- **Constraints:** synthetic evidence only; no authentication; one process with in-memory jobs.
- **Assumptions:** the onset estimate (first ERROR-log episode on the affected services, capped at the reported
  time) approximates the true onset. The provider's per-request overhead stays within the input-budget allowances.
- **Limitations:** see [ARCHITECTURE.md](ARCHITECTURE.md), "Known limits", and [TECHNICAL_NOTE.md](TECHNICAL_NOTE.md).
  - Code checks citation structure, not meaning: the model can misfile evidence, as in run 4.
  - The noise-floor fix and the leak-free prompt have no real-model run.
- **Future improvements:**
  - a live evaluation of both approaches across all 10 cases, with repeated runs;
  - authentication and persistence;
  - real observability integrations.

## 11. Traceability matrix (assignment requirement → implementation, tests, documentation)

| Assignment requirement | Implementation | Tests | Documentation | Status |
|---|---|---|---|---|
| Six distinct agents with structured collaboration | `app/agents/*.py`, `contracts.py` | `tests/test_agents.py` | ARCHITECTURE "Agents" | Verified |
| Five structured evidence tools, error handling, simulated failures | `app/tools/`, `faults.py` | `tests/test_tools.py` | TOOLS.md | Verified |
| Parallel execution, shared state, task dependencies, feedback loop | `app/orchestration/engine.py`, `critic.py` | `test_independent_tasks_run_concurrently`, `test_follow_up_tasks_are_added_and_run_once` | ARCHITECTURE diagrams | Verified |
| Bounded retries, limits, termination, failure recovery | `engine.py`, `service.py`, `base.py` | `test_retryable_failure_is_retried_until_success`, `test_engine_deadline_still_produces_a_report` | ARCHITECTURE "Termination controls" | Verified |
| Ranked hypotheses, supporting/contradicting evidence, confidence, facts vs assumptions, inconclusive | `reasoning.py`, `report_rules.py`, `adjudication.py` | `tests/test_phase6_quality.py` | ARCHITECTURE, report view | Verified |
| ≥ 10 synthetic incidents incl. misleading, conflicting, tool failures | `data/eval/` | `tests/test_eval_dataset.py` | data/eval/README.md | Verified |
| Multi-agent vs single-agent baseline with the same tools | `app/baseline/single_agent.py` | `tests/test_baseline.py` | EVALUATION.md | Verified offline |
| Accuracy, grounding, tool calls, latency, tokens | `app/evaluation/metrics.py`, `pricing.py` | `tests/test_eval_metrics.py`, `tests/test_live_evaluation.py` | EVALUATION.md | Partial: tokens and cost only in live mode, never run |
| Evaluation comparison | `evaluation_results/offline-heuristic/`; live runner prepared (`app/evaluation/live.py`) | `tests/test_live_evaluation.py` | EVALUATION.md | Partial: offline stand-in only; the live comparison is prepared but not run |
| Runnable implementation and setup | README | full suite | README | Verified on macOS; Windows steps not run |
| Architecture diagram and explanation | — | — | ARCHITECTURE "Diagrams" | Verified (Mermaid parsed and rendered with Mermaid 10.9.1) |
| Synthetic dataset and automated tests | `data/`, `tests/` | 626 tests | README | Verified |
| Technical note ≤ 500 words | — | — | TECHNICAL_NOTE.md | Verified |
| AI coding assistant transcript | — | — | AI_TRANSCRIPT.md | Verified: redacted, provenance stated |
| No hardcoded answers | labels isolated; prompt check | `test_no_model_facing_prompt_reveals_an_evaluation_answer` | this document, NFR-06 | Verified |
| Incomplete functionality and dependencies documented | — | — | README "Limitations", section 10 | Verified |
