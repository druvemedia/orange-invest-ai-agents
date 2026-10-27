# Live run artifacts: EVAL-01, run 4 (2026-10-27, after the run 3 fixes)

Unmodified copies of the outputs of the fourth completed billed investigation of EVAL-01 (`inv-7372f0aaa41c`).
Runs 1–3 are in `../EVAL-01/`, `../EVAL-01-run2/` and `../EVAL-01-run3/` and were not modified. Checksums are in
`SHA256SUMS`.

| File | What it is |
|---|---|
| `live_job.json` | `GET /investigations/inv-7372f0aaa41c`: status, timestamps, outcome, model usage |
| `live_report.json` | the report view, with evidence classes, adjudication, inventory and timeline checks |
| `live_report.md` | the same report as Markdown |

## Provenance

- **Model:** `gpt-4o-mini` through the product API. The code was commit `1f62e98` plus the uncommitted run 3 fixes in
  `app/agents/report_rules.py`, `app/agents/grounding.py` and `app/agents/reasoning.py`:
  - the top hypothesis names the change it cites;
  - no "increased load" support from latency alone when the request rate is flat;
  - a prompt line asking for the change to be named in the title.
- **Limits:** the same pinned limits as run 3: at most 16 requests, a 64,000 input budget and 4,000 output tokens per
  request, a 300 s deadline and 360 s hard stop, and one active investigation. One submission.
- **Usage:** 9 model calls (the dependency step needed a second attempt). 59,987 input and 5,199 output tokens,
  74.2 s server-side, about $0.012. The largest input bound was 44,639 for 11,707 actual tokens. No request was
  refused.
- **Before copying:** scanned for keys, authorization headers, environment-variable names, emails, IP addresses and
  local paths. Nothing was found. The server log was not copied.

## What it shows (one observation, not a benchmark)

**Disclosure: this run's Reasoning prompt revealed EVAL-01's answer.** A new prompt line gave the example "Pool size
cut 40 to 8 by CHG-1", which mirrors this case's root cause. The model's well-named title may have been primed by
that example, so it is **not clean evidence** that the model, or the fix, names causes better. The example was found
in the final audit and replaced with a neutral one (`tests/test_prompt_leakage.py` now checks every prompt against
every label). This run was not repeated.

- **Conclusion:** `inconclusive`. The top hypothesis is "DB Connection Pool Size Reduced from 40 to 8 by CHG-E01-2"
  (medium). The model named the cause itself, possibly primed by the leaked example (see the disclosure above), so
  the code's title rule did not need to act.
- **Against the answer key:** `missed_inconclusive`. Top-1 matches and key-evidence recall is 2/2, but an
  inconclusive result is a miss for this case.
- **Why it stayed inconclusive:**
  - **A model citation error.** The model filed the timeout logs ("Timed out waiting for DB connection from pool")
    as evidence *against* H1, and attached the change record to H2 instead of H1. So the direct-support check found
    one independent observation, and the contradiction check found direct evidence against H1. Code does not
    judge whether a log line semantically supports a hypothesis, so it could not correct this.
  - **A code defect, fixed after this run.** One boundary sample in the error-rate baseline (7.61% at 10:00, before
    the 10:02 onset estimate) inflated the noise floor, so a jump from 0.2% to 7.61% was classified "within the
    normal range". Steps are now taken within the middle half of the baseline only
    (`tests/test_run3_fixes.py::test_run4_error_rate_jump_is_a_change_not_noise`). It did not change this run's
    conclusion: H1 did not cite the error-rate sample.
- **Fixes working as intended:**
  - The model's metrics window started at onset, so the fetch was widened to 09:12, and the pool was classified
    "100 vs baseline 38.4: changed".
  - The "increased load" hypothesis is low, with the flat request rate cited against it. The saved report does
    not record whether the code's load rule or the model put it there.
  - The duplicated round-limit limitation was removed.
