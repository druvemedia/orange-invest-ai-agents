# Live run analysis: EVAL-01 (2026-10-27), Phase 6 fixes and run 2

## Run 1 (measured)

One billed investigation through `POST /investigations`. It was the first live run of EVAL-01 and the second billed
run overall (run 2 is below); the first was INC-2041 through the CLI on 2026-10-26 (see docs/ARCHITECTURE.md).

| | |
|---|---|
| Job | `inv-f60feafed333` |
| Model | `gpt-4o-mini` |
| Duration | 67.7 s server-side |
| Model calls | 9 for 8 agent tasks: one internal retry, inferred because `critic.r2` was called twice with identical input size (8,739 tokens). The retry's cause was not recorded. |
| Tokens | 53,390 input, 5,493 output |
| Result | `completed`, but the conclusion was **inconclusive** with the correct cause (pool cut 40 → 8) ranked first at high confidence |

Scored against the gold label, the outcome was `missed_inconclusive` (top-1 hit, key-evidence recall 2/2).

The artifacts are preserved, byte for byte, in **`artifacts/live-runs/EVAL-01/`**: `live_report.md`,
`live_report.json` and `live_job.json`, with a `SHA256SUMS` file and a README.
- In Phase 7 they were copied from the session scratchpad. Each copy was compared with `cmp`, and all three
  checksums match the ones recorded in Phase 6. The scratchpad originals were left untouched.
- Before copying, they were scanned for API keys, authorization headers, environment-variable names, emails,
  phone numbers, IP addresses and local paths. Nothing was found, so no sanitised derivative was needed.
- The run's server log was deliberately not copied: it isn't needed, and it contains local process details.

**This is a single observed model run, not a benchmark.** It shows what one model did once on one synthetic case.

SHA-256 checksums:

| File | SHA-256 |
|---|---|
| `live_report.md` | `edd02b13…6764351` |
| `live_report.json` | `a5acef17…a19cca` |
| `live_job.json` | `a65eb5c7…e951c22` |

## Defects, root causes and fixes

| # | Defect in the live report | Root cause | Fix (Phase 6) |
|---|---|---|---|
| 1 | Correct cause ranked first, but the conclusion was inconclusive | `conclusion_allowed` required the critic's final verdict to be `accept`. A critic still saying "revise" at its round limit forced inconclusive whatever the evidence. | `app/agents/adjudication.py`: an explicit, recorded policy. The cause is named only if all checks pass (direct support from ≥ 2 sources, no material contradiction, no comparable alternative, follow-ups obtained, review completed). Critic issues without direct evidence are listed as unresolved but don't block. |
| 2 | A flat request rate (≈120 rps) cited as support for "increased load" | Every tool record counted as direct evidence; nothing looked at metric values | Evidence classes: a metric is *direct* only if it deviates from its series' baseline. In Phase 6 that meant by at least 25%; Phase 7 refined it to max(25%, 3 × baseline noise) against a median baseline. A *no_change* metric is removed from support, or moved to contradicting when the hypothesis claims an increase. |
| 3 | A dependency edge (payments-api → fraud-check) supported a fraud-check latency hypothesis at medium confidence | Same rule: topology counted as direct support | Dependency edges and past incidents are *contextual*. Without direct support, confidence is capped to low, and the critic raises a rule issue that the model can't overrule. |
| 4 | The report said request-rate and PostgreSQL metrics were unavailable, but both were retrieved | The reporting model saw only per-task outcomes, and its limitations were never checked | An authoritative evidence inventory built from tool outcomes (retrieved, partial, no matches, failed, timed out, not run; signals retrieved). Unavailability claims about retrieved signals are removed and recorded. |
| 5 | Interpretation inside facts ("…indicating a saturated pool causing timeouts"); empty assumptions | The report had no interpretations section, and nothing checked facts for causal language or flagged an empty assumptions list | New `interpretations`, `would_confirm` and `would_disconfirm` report fields. Facts with causal or inferential language move to interpretations. If no assumptions are given while a causal hypothesis exists, the causal-link premise is added, marked as added by code. |
| 6 | Pool utilisation 102% | The dataset builder applied ±2% jitter after a 100% level | Percentages are clamped to 0–100, and the dataset was regenerated. The signal (38% → 100%) is unchanged. |

## Verification (offline only)

- `tests/test_phase6_quality.py` (39 tests) reproduces the live run's mistakes with a scripted model on EVAL-01's
  real evidence. It checks that each defect is now caught: the flat rate moved to contradicting, the topology
  hypothesis capped to low, the cause accepted on evidence despite a "revise", both false claims removed, the
  causal "fact" moved, and an assumption added.
- Unit tests cover the evidence classes, inventory statuses, claim validation, every adjudication outcome, the
  report rules and the dataset bound.
- Breaking each fix on purpose made tests fail.
- A real-browser check of the updated report page passed 33 of 33 checks.

## Phase 7 follow-up

The live run's report could also have blamed a change that came after the symptoms, or counted an echo of a
change as corroboration. EVAL-01 had neither problem, but EVAL-02 and EVAL-10 test them. Phase 7 added:
- deterministic timeline checks: a change after the first symptom, or one whose rollback didn't end the
  symptoms, can't be named as the cause of onset;
- evidence-independence grouping: echoes, duplicates and same-trace records are one observation;
- a noise-aware metric baseline.

These are tested offline only (`tests/test_phase7_temporal_independence.py`).

For EVAL-01 the timeline check reports **ordering ambiguous**, not a violation. The pool-utilisation sample at
09:55 already shows saturation in the same 5-minute sampling interval as CHG-E01-2 at 09:55, so their order
can't be determined. Ambiguous timing doesn't block the cause.

## Run 2, after the Phase 6–9 fixes (measured)

One more billed investigation of EVAL-01 through `POST /investigations`, from commit `9abc762` (the third billed run
overall). Outputs are preserved unchanged in `artifacts/live-runs/EVAL-01-run2/`; run 1's artifacts were not touched.
The gold label was read only after the run finished.

| | |
|---|---|
| Job | `inv-ffb2e49e8b55` |
| Model | `gpt-4o-mini` |
| Duration | 55.9 s server-side |
| Tasks / attempts | 14 / 14, none failed, no retries |
| Model calls | 8, all HTTP 200 (orchestrator, dependency, observability, reasoning ×2, critic ×2, reporting) |
| Tokens | 43,859 input, 3,989 output |
| Report author | model gpt-4o-mini (not the deterministic fallback) |
| Conclusion | **inconclusive**; final critic verdict "revise" at the round limit |

**What this shows.** The live API accepted the extended structured-output schema, and the model filled the
interpretations, would-confirm and would-disconfirm fields. Every citation resolved.

**Adjudication.**
- Failed: `hypotheses_available`, `leader_confident` and `leader_clearly_ahead`. The reasoning model set
  `insufficient_evidence`, so code capped every hypothesis to low.
- Passed: `direct_support` (5 observations from 2 independent sources, changes and logs), `no_material_contradiction`,
  `no_credible_alternative`, `follow_ups_obtained` (1 follow-up, no matches) and `temporal_consistency` (CHG-E01-2 at 09:55
  precedes the first symptom at 10:02 in the retrieved logs).

**Against the gold label:** top-1 and top-3 correct, key-evidence recall 2/2, outcome `missed_inconclusive`, the same as
run 1. HTTP success and schema acceptance did not produce a correct conclusion.

**Defects observed** (the first is fixed offline since; the others are documented only):
- **Baseline contamination.** The model scoped the metrics query to start at onset (10:00), so every pool sample was
  post-incident. 100% utilisation was classified `no_change` and listed as *contradicting* the correct hypothesis.
  The case data has 37–38% before onset. The cause was an earliest-samples baseline that code never widened.
  *Fixed after this run, offline only:* metrics are now fetched from at least 60 min before the reported time,
  and the baseline uses only samples before the estimated onset. The same window now gives "100 vs baseline
  38.4: changed", or "insufficient baseline" when no earlier samples exist (`tests/test_metric_baseline.py`).
  This has not been re-checked with a real model.
- **Weak citations.** One hypothesis about "dependent services" cites an interpretation about the config change, and
  another cites the unrelated orders deploy as support.
- **Over-eager fact checks.** Two facts were moved to interpretations by mistake: "indicate" matches the causal-language
  check, and "not rapidly increasing" matches the increase check, which ignores negation.
- **Report noise.** The limitation "Critic round limit reached" appears twice. One open question asks which metrics
  were missing, although the inventory shows none were.

## Not verified

- Whether the Phase 6–9 rules make real-model conclusions correct more often. Two runs of one case cannot show that,
  and neither run named a cause.
- Any real-model comparison of the multi-agent system with the single-agent baseline.

Offline heuristic scores changed (docs/EVALUATION.md), but they measure a naive rule, not the model.
