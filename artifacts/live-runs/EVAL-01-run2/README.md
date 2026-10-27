# Live run artifacts: EVAL-01, run 2 (2026-10-27, after the Phase 6–9 fixes)

These are unmodified copies of the outputs of the second billed investigation of EVAL-01, and the third billed run
overall. The first EVAL-01 run is in `../EVAL-01/` and was not modified. Checksums are in `SHA256SUMS`.

| File | What it is |
|---|---|
| `live_job.json` | `GET /investigations/inv-ffb2e49e8b55`: status, timestamps, outcome, model usage |
| `live_report.json` | the report view (Phase 6–9 schema: adjudication, inventory, timeline checks, interpretations) |
| `live_report.md` | the same report as Markdown |

## Provenance

- **Model:** `gpt-4o-mini` through the product API, from commit `9abc762`.
- **Input:** `data/eval/cases/EVAL-01/incident.json`, with evidence source `EVAL-01`.
- **Usage:** 8 model calls, all HTTP 200, with no retries. 43,859 input and 3,989 output tokens. 55.9 s server-side.
- **Before copying:** scanned for keys, authorization headers, environment-variable names, emails, IP addresses and
  local paths. Nothing was found, and the files are copied unchanged.
- **Server log excluded:** the server log was not copied.

## What it shows (one observation, not a benchmark)

- **Schema:** the extended report schema was accepted by the live API, and the report is model-written, not the
  deterministic fallback.
- **Outcome:** inconclusive again. The correct cause (pool reduction) is ranked first with both key evidence items
  cited (`missed_inconclusive` against the label).
  - The reasoning model marked the evidence insufficient, so all hypotheses were capped to low and the
    adjudication's confidence checks failed.
  - The direct-support, timeline, contradiction and follow-up checks passed.
- **Observed defect:** the model scoped the metrics window to start at onset (10:00). Every retrieved metric
  sample was therefore post-incident, and 100% pool utilisation was classified "no_change" and shown as
  *contradicting* evidence. This is the documented limitation of an earliest-samples baseline.

Analysis: `docs/LIVE_RUN_EVAL01_ANALYSIS.md`.
