# Technical note: multi-agent incident investigation

**Problem.** Given an incident report, gather evidence and produce ranked, evidence-backed root-cause
hypotheses. Say "inconclusive" when the evidence does not justify a cause.

**Design.** Six agents run as tasks on a bounded asyncio engine. The engine handles dependencies, retries for
transient errors only, per-attempt timeouts, task, attempt and deadline budgets, and guaranteed termination:
- the **Orchestrator** scopes the incident and plans evidence tasks;
- **Observability** (logs, metrics) and **Dependency** (changes, topology) turn tool results into cited findings;
- **Reasoning** proposes competing hypotheses;
- the **Critic** challenges them and may request validated follow-up queries, for at most 2 rounds;
- **Reporting** writes the report.

Agents exchange only typed results. Every citation must resolve to a record retrieved by one of five
deterministic tools. The model cannot execute tools; it requests queries that code validates.

**Safeguards enforced in code:**
- Evidence classes: logs, changes and deviating metrics are direct. Topology, past incidents and flat metrics
  are context.
- Confidence caps.
- Duplicate or echoed records are merged before corroboration is counted.
- Timeline checks: a change after the first symptom, or one whose rollback did not stop the symptoms, cannot
  be named as the cause.
- An inventory of what was actually retrieved, used to remove false "missing evidence" claims.
- Facts kept separate from interpretations and assumptions.
- An auditable adjudication that names a cause only when every check passes.

A single-agent baseline uses the same tools, budgets, rules and report contract, but has no critic.

**Evaluation.** There are 10 labelled synthetic incidents: misleading deploy, conflicting signals, missing
metrics, tool failure, competing causes, insufficient evidence and others. Gold labels are loaded only after all
runs finish. The metrics are:
- strict and lenient root-cause accuracy;
- confidently wrong conclusions;
- top-1 and top-3 hits;
- citation existence, usability and key-evidence recall;
- tool calls, model calls, latency and tokens.

**Results.** The offline evaluation uses a rule-based stand-in for the model, shared by both approaches, so it
tests the harness, not reasoning. Both approaches score strict 0.4, lenient 0.6 and confidently wrong 0.0.
These numbers do not measure real-model quality.

Two billed gpt-4o-mini runs predate the Phase 6–7 rules. Both ranked the correct cause first but concluded
inconclusive. The EVAL-01 run (67.7 s; 53,390 input and 5,493 output tokens) is preserved in
`artifacts/live-runs/EVAL-01/`. It exposed six defects, which were fixed and tested offline. These are
single observations, not a benchmark.

**Verification.** 462 offline tests pass without network access, and 35 real-browser UI checks pass.

**Limitations.**
- Evidence is synthetic; there is no authentication, and jobs are kept in memory.
- Semantic support beyond structural checks depends on the model.
- Thresholds (25%, 3× noise, ±60 s) are fixed, and keyword triggers can miss paraphrases.
- The Phase 6–7 rules have no real-model validation.
- No real-model comparison of the two approaches has been run.
