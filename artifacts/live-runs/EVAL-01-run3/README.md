# Live run artifacts: EVAL-01, run 3 (2026-10-27, after Phases 1–4)

Unmodified copies of the outputs of the third billed investigation of EVAL-01 (`inv-8787259b0818`). Runs 1 and 2
are in `../EVAL-01/` and `../EVAL-01-run2/` and were not modified. Checksums are in `SHA256SUMS`.

| File | What it is |
|---|---|
| `live_job.json` | `GET /investigations/inv-8787259b0818`: status, timestamps, outcome, model usage |
| `live_report.json` | the report view, with evidence classes, adjudication, inventory and timeline checks |
| `live_report.md` | the same report as Markdown |

## Provenance

- **Model:** `gpt-4o-mini` through the product API, from commit `1f62e98`. One submission, which completed.
- **Pinned limits** (that server process only; `.env` was not changed):
  - at most 2 critic rounds, so at most 16 model requests;
  - an input budget of 64,000 and an output limit of 4,000 tokens per request;
  - a 120 s request timeout, a 300 s deadline and a 360 s hard stop;
  - one active investigation.
- **Usage:** 10 model calls. The dependency step and the second reasoning round each needed a second attempt. 83,624 input and 5,933 output tokens, 84.8 s server-side. At $0.15 / $0.60 per million tokens, about $0.016.
- **Input budget:** every call was checked before sending, and none was refused. The largest bound was 52,681
  tokens (82% of the budget), for 14,520 actual input tokens. Every call's actual input was below its bound.
- **Before copying:** scanned for keys, authorization headers, environment-variable names, emails, IP addresses and
  local paths. Nothing was found, and the files are copied unchanged. The server log was not copied.
- **An earlier attempt was cancelled:** `inv-ef5d980add5c` was submitted minutes before and cancelled when its
  command was interrupted. It made 3 model calls (9,342 input and 1,260 output tokens, about $0.002). Its record is
  kept outside the repository.

## What it shows (one observation, not a benchmark)

- **Conclusion:** `probable_cause_identified`. This is the first real-model run to name a cause. All nine decision
  checks passed. The critic's final verdict was still "revise", with 8 unresolved issues, none citing direct
  evidence against the leader.
- **Leading hypothesis (H1):** "Database Connection Pool Saturation" (high). It cites the pool at "100 vs baseline
  38.4 (+161%): changed", and the config change that cut the pool from 40 to 8.
- **Against the answer key:** `wrong_conclusion`. The label requires the hypothesis to name the cause (pool size,
  reduction or config). H1's title and assessment name the mechanism, saturation; only its citations and the
  report summary tie it to the config change. Key-evidence recall is 2/2, and the distractor was not ranked first.
- **Not exercised in this run:**
  - The model's metrics window started at 09:00, before onset, so the metrics fetch was not widened. The old
    earliest-third baseline would also have given about 38%.
  - No fact used "indicate" or a negated increase, so those rules did not apply.
  - The rule removing a duplicated critic round-limit limitation did apply.
- **Weak grounding not caught by code:** H2, "Increased Load on Payments API" (medium), is supported by a latency
  increase, not by request-rate evidence. The request rate was flat.

Analysis: `docs/LIVE_RUN_EVAL01_ANALYSIS.md` (run 3 is not yet written up there).
