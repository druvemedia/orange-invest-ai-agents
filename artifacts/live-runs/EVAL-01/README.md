# Live run artifacts: EVAL-01, 2026-10-27

These are unmodified copies of the outputs of the **first** billed real-model investigation of EVAL-01 (the second is in `../EVAL-01-run2/`). Their
SHA-256 checksums are in `SHA256SUMS` and match the originals recorded at the time.

It was the second billed run overall. The first, of INC-2041 through the CLI on 2026-10-26, is summarised in
`docs/ARCHITECTURE.md` ("Model provider"); its outputs are not preserved in the repository.

| File | What it is |
|---|---|
| `live_job.json` | `GET /investigations/inv-f60feafed333`: status, timestamps, outcome, model usage (9 calls, 53,390 input / 5,493 output tokens) |
| `live_report.json` | `GET /investigations/inv-f60feafed333/report`: the report view as it was produced then (pre-Phase-6 schema) |
| `live_report.md` | `GET /investigations/inv-f60feafed333/report.md`: the same report as Markdown |

## Provenance

- **Model:** `gpt-4o-mini`, through the product API (`POST /investigations`).
- **Input:** the synthetic incident `data/eval/cases/EVAL-01/incident.json`, with evidence source `EVAL-01`.
- **Pre-Phase-6 data:** the run used the dataset as it was then, which included the 102% pool-utilisation sample
  that Phase 6 later fixed.
- **Before copying:** the files were scanned for API keys, authorization headers, environment-variable names,
  emails, phone numbers, IP addresses and local paths. Nothing was found, and the files are copied unchanged.
- **Server log excluded:** the server log of that run was not copied. It isn't needed (its per-call figures are
  summarised in `docs/LIVE_RUN_EVAL01_ANALYSIS.md`) and it contains local process details.

## How to read them

This is **a single observed model run**, not a benchmark. It shows what one model did once on one synthetic case,
and it motivated the Phase 6 and 7 fixes. It doesn't measure accuracy, variance or cost per incident, and the
reports here predate those fixes. Analysis: `docs/LIVE_RUN_EVAL01_ANALYSIS.md`.
