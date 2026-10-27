# Live run analysis: EVAL-01 (2026-10-27), four real-model runs

Four completed billed investigations of EVAL-01 with `gpt-4o-mini`, each through `POST /investigations` and each a
single observation. They are scored against the unchanged answer key (`data/eval/labels.jsonl`) with the project's
own scorer. A fifth submission (`inv-ef5d980add5c`) was cancelled after 3 model calls when its command was
interrupted; it produced no report.

| Run | Code | Conclusion | Top hypothesis (confidence) | Answer key | Calls | Tokens in / out | Time | Cost |
|---|---|---|---|---|---|---|---|---|
| 1 | before Phase 6 | inconclusive | Connection Pool Saturation Due to Reduced Pool Size (high) | missed (inconclusive) | 9 | 53,390 / 5,493 | 67.7 s | $0.011 |
| 2 | Phases 6–9 | inconclusive | Insufficient Database Connection Pool Size (low) | missed (inconclusive) | 8 | 43,859 / 3,989 | 55.9 s | $0.009 |
| 3 | + baseline, wording and budget fixes | **probable cause identified** | Database Connection Pool Saturation (high) | **wrong conclusion** | 10 | 83,624 / 5,933 | 84.8 s | $0.016 |
| 4 | + run 3 fixes | inconclusive | DB Connection Pool Size Reduced from 40 to 8 by CHG-E01-2 (medium) | missed (inconclusive) | 9 | 59,987 / 5,199 | 74.2 s | $0.012 |

Every run ranked the pool-size change, or its direct effect, first, and every run cited both key evidence items
(the change record and a pool log line). **No run has yet produced the correct result: the cause named and
accepted.** Runs 1, 2 and 4 named the cause but did not conclude. Run 3 concluded, but its title named the
mechanism, not the cause. Run 4's prompt contained an example that mirrored EVAL-01's answer (see run 4), so its
well-named title is not clean evidence. Costs are at $0.15 / $0.60 per million input / output tokens.

## Run 1 (measured)

One billed investigation through `POST /investigations`. It was the first live run of EVAL-01 and the second billed
run overall (runs 2–4 are below); the first was INC-2041 through the CLI on 2026-10-26 (see docs/ARCHITECTURE.md).

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
- **Over-eager fact checks** (fixed offline since). Three facts were moved to interpretations. Two contained
  "indicate": one restates the log message, and the other ("a 504 error indicating 'payments-api' was not
  responsive") lightly glosses "Upstream payments-api returned 504". The third, "was not rapidly increasing", was
  read as a claimed increase because negation was ignored. Code now keeps all three as facts
  (`tests/test_report_wording.py`).
- **Report noise.** The critic round limit was stated twice: by code ("adjudicated on the evidence") and by the model
  ("not resolved due to insufficient evidence"). The model's restatement is now dropped when code states it
  (fixed offline). Not fixed: one open question asks which metrics were missing, although the inventory shows none
  were, because open questions are not checked against the inventory.

## Run 3, after the baseline, wording and budget fixes (measured)

`inv-8787259b0818`, from commit `1f62e98`; artifacts in `artifacts/live-runs/EVAL-01-run3/`.

- **Decision:** all nine decision checks passed, so code accepted H1, "Database Connection Pool Saturation" (high).
  The critic's final verdict was still "revise", with 8 unresolved issues, none citing direct evidence against H1.
- **Metric baseline:** pool utilisation was "100 vs baseline 38.4 (+161%): changed". The model's window started at
  09:00, before onset, so the baseline fix was not needed in this run.
- **Answer key: `wrong_conclusion`.** The label requires the cause (pool size, reduction or config). H1 named the
  mechanism; only its citations and the summary tied it to the config change.
- **Weak grounding:** "Increased Load on Payments API" (medium) was supported only by a latency increase, while
  the request rate was flat.

**Fixed after run 3, offline only:**
- The top hypothesis now names the change it cites when its title does not
  (`app/agents/report_rules.py::name_cited_change`). The Reasoning prompt also asks the model to name the change.
- A load claim cannot rest on symptom metrics when every request-rate sample shows no change. Those citations are
  removed from support, and the flat request-rate peak is added against the claim (`app/agents/grounding.py`).
- Tests: `tests/test_run3_fixes.py`.

## Run 4, after the run 3 fixes (measured)

`inv-7372f0aaa41c`, from commit `1f62e98` plus the uncommitted run 3 fixes; artifacts in
`artifacts/live-runs/EVAL-01-run4/`.

**Disclosure: this run's Reasoning prompt revealed EVAL-01's answer.** A new prompt line gave the example "Pool size
cut 40 to 8 by CHG-1", which mirrors this case's root cause. The model's well-named title may have been primed by
that example, so it is **not clean evidence** that the model, or the fix, names causes better. The example was found
in the final audit and replaced with a neutral one (`tests/test_prompt_leakage.py` now checks every prompt against
every label). This run was not repeated.

- **Worked as intended:**
  - The model titled H1 "DB Connection Pool Size Reduced from 40 to 8 by CHG-E01-2" itself, but possibly primed by
    the leaked example.
  - The window started at onset, so the metrics fetch was widened to 09:12, and the pool was again "changed vs 38.4".
  - The load hypothesis was low, with the flat request rate cited against it. The saved report does not record
    whether the code's load rule or the model put it there.
  - The duplicated round-limit limitation was removed.
- **Conclusion: inconclusive** (answer key: `missed_inconclusive`). Three checks failed:
  - `direct_support`: one independent observation.
  - `no_material_contradiction`: the model filed the timeout logs ("Timed out waiting for DB connection from pool")
    as evidence *against* H1, and attached the change record to H2. Code does not judge what a log line means, so
    this model error decided the result.
  - `leader_clearly_ahead`: H2 and H4 were as confident as H1 when it was checked.
- **A code defect found in this run, fixed offline:** one boundary sample in the error-rate baseline (7.61% at
  10:00, before the 10:02 onset estimate) inflated the noise floor, so a jump from 0.2% to 7.61% read as "within the
  normal range". Steps are now taken within the middle half of the baseline only
  (`test_run4_error_rate_jump_is_a_change_not_noise`). H1 did not cite that sample, so this did not decide run 4.
- **Input budget:** the largest bound was 44,639 for 11,707 actual tokens. No request was refused.

## Not verified

- Whether the fixes make real-model conclusions correct more often. Four runs of one case cannot show that, and
  none was scored correct.
- The noise-floor fix with a real model. It was found in run 4 and fixed afterwards, and no run has used it.
- Any real-model comparison of the multi-agent system with the single-agent baseline.
- Run-to-run variation. The same case gave different leading titles, confidences and citations each time.
- Any real-model run with the leak-free Reasoning prompt. The fixed prompt has not been sent to a model.

Offline heuristic scores changed (docs/EVALUATION.md), but they measure a naive rule, not the model.
