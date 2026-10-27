# Evaluation: multi-agent system vs single-agent baseline

## What is compared

| | Multi-agent (`app/agents`) | Single-agent baseline (`app/baseline`) |
|---|---|---|
| Model access | `LLMClient` (same client, model and structured outputs) | same |
| Evidence | the five registered tools over the same case data and simulated faults | same |
| Tool choice | Orchestrator plans a fixed sweep; Critic may request validated follow-ups | the model chooses calls each step, validated the same way (registered tool, schema-valid query, no repeats) |
| Grounding rules | references must have been shown; failed evidence is never support; evidence classes, citation checks and confidence cap; inventory-validated limitations; facts vs interpretations | same code (`report_rules.finalize_report`, `apply_citation_checks`, `cap_confidence`, `SHARED_RULES`) |
| Conclusion rule | `adjudicate` (Phase 6): leader ≥ medium and strictly ahead, direct support from ≥ 2 sources, no material contradiction, no comparable alternative, requested follow-ups obtained, and a completed critic review | the same checks, minus the review check (there is no critic) |
| Budgets | ≤ 40 tasks, 2 attempts per agent task, 2 critic rounds, 3 requests per round, deadline 300 s | ≤ 6 steps, ≤ 14 tool calls (≤ 6 per step), 2 attempts per step, same per-step timeout and deadline |
| Output | `ReportOutput` | `ReportOutput` (the same contract) |

The baseline is a genuine agent, not a one-shot prompt: it plans, calls tools, sees the results, follows up and
reports. Known asymmetries:
- **Tool calls.** The multi-agent system always runs its initial sweep of 5+ calls. The baseline decides for itself and
  may call fewer or more tools (up to 14).
- **Critic condition.** The critic-acceptance condition makes the multi-agent system more conservative by design.
  That is part of the architecture being evaluated.
- **Prompts.** The two prompts differ by necessity: six role prompts versus one investigator prompt. Both include the
  same shared evidence rules.

## Running

```zsh
python -m app.evaluation offline                     # free, deterministic; writes evaluation_results/offline-heuristic/
python -m app.evaluation live --confirm-billed        # real model calls (billed); writes evaluation_results/live/
python -m app.evaluation live --confirm-billed --cases EVAL-01,EVAL-09 --approach single_agent
```

Without `--confirm-billed`, live mode prints the scope (the number of investigations) and the only measured usage
figure (one multi-agent run, about 55k input / 5.7k output tokens, 104 s), then exits with code 2 without making a
request. Runs are sequential, with a fresh client per run so token usage is attributed to that run.

**Offline mode** replaces the model with `HeuristicLLMClient`, a rule set shared by both approaches: the most recent
change is the cause, otherwise the most frequent error. It exercises every code path deterministically. **Its scores
measure the harness and the naive rule, not LLM reasoning, and not the architectures.** Identical offline scores for
both approaches are expected.

## Metrics (exact definitions)

Each case is run once per approach. A run that raises is recorded with `status = failed`, never dropped.

**Outcome** per run, from the report's `conclusion` and its top-ranked hypothesis. A hypothesis *matches* a label if,
for each keyword group in `match_all`, at least one term occurs (case-insensitive substring) in its title or assessment.

| Outcome | Rule |
|---|---|
| `correct` | a root cause is expected, the conclusion is `probable_cause_identified`, and the top hypothesis matches the gold label or an acceptable alternative |
| `correct_inconclusive` | inconclusive is expected and the conclusion is `inconclusive` |
| `acceptable_inconclusive` | a root cause is expected, the conclusion is `inconclusive`, and the label allows inconclusive |
| `missed_inconclusive` | as above, but the label does not allow inconclusive |
| `wrong_conclusion` | a probable cause is named but the top hypothesis does not match, or a cause is named when inconclusive is expected |
| `failed` | no report: the run crashed |

- **Strict accuracy** = (`correct` + `correct_inconclusive`) / runs.
- **Lenient accuracy** = strict + `acceptable_inconclusive`, divided by runs.
- **Wrong-conclusion rate** = `wrong_conclusion` / runs: being confidently wrong, the most harmful outcome.
- **Failure rate** = `failed` / runs.
- **Top-1 / top-3 hit rate** (cases with a gold root cause): the first, or any of the first three, ranked hypotheses
  match, regardless of the conclusion.
- **Distractor ranked first** (gold cases): the top hypothesis matches a labelled distractor and not the gold label.
- **Must-consider coverage**: the fraction of `must_consider` patterns matched by any ranked hypothesis (EVAL-08 only).
- **Key-evidence recall** (gold cases): the fraction of the label's `key_evidence` matchers satisfied by records cited
  in the top hypothesis's supporting references. Findings are followed to their records. The matched alternative's
  matchers are used when it has any; otherwise the gold label's.

**Grounding**, over all references in the ranked hypotheses (supporting and contradicting) and the facts:
- **Existence rate**: the reference resolves to evidence collected in that run (a tool record, a tool result or an
  agent finding).
- **Availability rate**: the reference resolves to usable evidence. A tool result that failed or was never collected
  doesn't count, and a finding counts only if all the references it cites are usable.
- **Lexical support rate** (*heuristic*): of the supporting references that resolve to records, the share whose record
  text shares at least one content word (four or more characters, not a stopword) with the hypothesis. This is a weak
  proxy for "the evidence supports the claim", and it can't judge meaning. Key-evidence recall is the stronger
  signal, and manual review of `results.json` is recommended for live runs.

**Cost and speed**:
- **Tool calls**: evidence-tool executions, including retries and failed calls.
- **Model calls**: `generate()` calls, including retries.
- **Invalid outputs**: model outputs rejected by schema or grounding checks.
- **Latency**: wall-clock seconds for the whole investigation (`perf_counter`), with runs sequential.
- **Tokens**: input and output tokens as reported by the provider, summed per run. Shown as "n/a" when the client
  reports none (offline), and never estimated.

## Outputs

`evaluation_results/<mode>/` holds three files:
- `results.json`: meta, aggregates, per-run scores, normalised runs with evidence maps, and labels.
- `per_case.csv`
- `report.md`: aggregate table, per-case table and gold labels.

## What the results can and cannot show

- **Offline results** (committed under `evaluation_results/offline-heuristic/`) show that the harness, dataset, metrics
  and both pipelines work end to end, and how the metrics react. For example, the naive rule ranks the distractor
  first on EVAL-02, 04, 05 and 10. They are **not** a quality measurement of either approach.
- **Phase 6 changed the offline scores.** This comes from the stricter adjudication (direct support from ≥ 2
  sources) and the capped percentages in the dataset. For both approaches: strict accuracy 0.6 → 0.4, wrong
  conclusions 0.4 → 0.2, lenient accuracy unchanged at 0.6.
  - The naive rule now stays inconclusive when it can cite only a change record:
    - EVAL-03: correct → missed inconclusive;
    - EVAL-07: correct → acceptable inconclusive;
    - EVAL-04: wrong → acceptable inconclusive;
    - EVAL-05: wrong → missed inconclusive.
  - EVAL-02 and EVAL-10 remain confidently wrong. Their distractor deploys also have same-service error logs, and
    rejecting them needs reasoning about timing and the rollback that the rule doesn't do.
  - These changes say nothing about real-model accuracy.
- **Phase 7 changed them again**, through the timeline checks (`app/agents/temporal.py`). For both approaches,
  EVAL-02 and EVAL-10 moved from confidently wrong to missed inconclusive, so the wrong-conclusion rate is now
  0.0 (was 0.2). Strict accuracy (0.4) and lenient accuracy (0.6) are unchanged.
  - EVAL-02: the blamed deploy follows the first symptom.
  - EVAL-10: the symptoms persisted after the deploy's rollback.
  - The naive rule still doesn't find the true cause in either case.
  - Evidence-independence merging and the noise-aware metric baseline caused no further score changes.
- **Phase 9** fixed two false-exclusion bugs in the timeline checks: a stray earlier error setting onset, and a
  rollback of an unrelated change counting as a revert. The rerun produced identical scores and per-case
  outcomes; only timestamps and latencies in the result files changed.
- **Live results** would measure real models on 10 small, synthetic, author-written cases, one run each. That is
  enough to spot large differences, not small ones. The dataset author also wrote both systems, so case design may be
  unconsciously biased. Keyword matching can misjudge paraphrases; check `results.json` by hand.
- **Status:** live evaluation has **not** been run. It needs explicit authorisation because it makes billed calls.
