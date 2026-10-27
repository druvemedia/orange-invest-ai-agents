# Live run analysis: EVAL-01 (2026-10-27) and Phase 6 fixes

## The run (measured)

One billed investigation through `POST /investigations`. It is the only live run of EVAL-01 and the second billed
run overall; the first was INC-2041 through the CLI on 2026-10-26 (see docs/ARCHITECTURE.md).

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

## Not verified

None of this has been tested against a real model. Specifically:
- whether gpt-4o-mini fills the new report fields (`interpretations`, `would_confirm`, `would_disconfirm`)
  sensibly, and whether the API accepts the extended schema. It uses the same field types as before;
- whether real reports now reach correct conclusions more often.

Offline heuristic scores changed (docs/EVALUATION.md), but they measure a naive rule, not the model.
