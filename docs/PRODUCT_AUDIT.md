# Product audit (Phase 5, 2026-10-27)

Baseline before Phase 5: 314 tests passing. Phase 3.2 and Phase 4 uncommitted, on top of commit `75357d8`.

## What a user could do before Phase 5

| Step | State |
|---|---|
| Submit an incident | **Not possible.** `python -m app.cli <ID>` accepted only the three IDs already in `data/fixtures/incidents.jsonl`. |
| Start an investigation | CLI only; it needs a real OpenAI key, blocks for about 100 s and offers no other interface. |
| Read the report | CLI prints a terse summary (titles and limitations), or `--json` dumps about 160 KB of raw state. Citations are bare IDs such as `ev.logs#3`; the reader can't see what the evidence says. |
| HTTP API | Only `GET /health`. |

## What already works end to end (reused, not rebuilt)

- Six agents on a bounded orchestration engine:
  - Tasks, attempts, deadline and critic rounds are all capped.
  - Every task ends in a final state, and a report always exists (a deterministic fallback if the model fails).
- Evidence grounding:
  - Citations must have been shown to the agent.
  - Failed evidence is never used as support.
  - Confidence is capped by code, and a probable cause needs the critic's acceptance (replaced in Phase 6 by an
    evidence-based adjudication; see docs/ARCHITECTURE.md).
  - Inconclusive results are first-class.
- Safety:
  - The model can't execute tools; it can only request validated queries.
  - Provider errors are mapped to fixed messages.
  - Handler errors are redacted.
  - Logs contain IDs and counts only.
- Live integration was verified once with gpt-4o-mini.
- Evaluation: 10 labelled cases, a baseline and a harness (offline).

## Product-critical gaps (in priority order)

1. **No submission path.** A user cannot investigate their own incident through any interface.
2. **No API for investigations.** A synchronous call would block about 100 s (up to the 300 s deadline), so it needs a
   bounded background job with status and result endpoints.
3. **Unreadable reports.** Citations aren't resolved to the evidence they point to. There's no separation of
   impact, immediate and follow-up actions, or missing evidence in a user-facing form, and no Markdown or HTML rendering.
4. **No keyless local run.** Without an API key nothing can be demonstrated. The deterministic offline model exists
   (Phase 4) but isn't selectable.
5. **Unbounded inputs.** Incident text has no length limits, which means unbounded prompt cost. Nothing caps
   concurrent investigations.
6. **Evidence scope is implicit.** Investigations always read `data/fixtures`. The 10 evaluation worlds aren't
   selectable, and this isn't explained to users.

## Reliability and security gaps found

- No concurrency limit, and no hard time bound around a whole investigation beyond the engine deadline.
- API error format undefined for investigation failures; any internal exception text could reach a response.
- Unchanged, and documented: synthetic evidence only (no real integrations); in-memory state (lost on restart);
  a timed-out tool thread finishes in the background.

## Plan (implemented in this phase, in this order)

1. A product layer (`app/product/`) that reuses `OrchestratorAgent` unchanged:
   - an evidence-source registry (`demo` plus the evaluation worlds, inputs only, never labels);
   - a report view that resolves every citation to the actual record;
   - a bounded in-memory job service.
2. A REST API:
   - `POST /investigations` (202 plus a job ID), `GET /investigations/{id}`, `GET /investigations/{id}/report` (JSON
     or Markdown), `GET /investigations`, `GET /evidence-sources`.
   - Bounded inputs, a concurrency cap (429), 503 when no model is configured, and safe, redacted errors.
3. `LLM_PROVIDER=offline-heuristic` for keyless local runs. Every response and report from it carries an explicit
   "not an AI model" notice.
4. CLI: submit your own incident file, choose an evidence source, and print the readable Markdown report.
5. A minimal single-page web UI served by FastAPI (no new dependencies), after the API is tested.

Out of scope, deliberately: databases, queues, auth, real observability integrations.
Each would need a demonstrated deployment target first.

## Status after Phase 5

All five plan items are implemented and tested: 350 tests passing (36 of them new for the product layer).

Verified:
- the real server (uvicorn) with the offline model, driven by `curl`: submit, poll, JSON and Markdown report,
  `422`, `404`;
- the API key never appears in the server log;
- the UI script passes a syntax check.

Not verified: the product API with the real OpenAI model (no paid calls were made in this phase).

## Phase 5.1: browser verification

**Method.** Headless Chromium (Playwright 1.62.1, reusing a copy already in the local npm cache; nothing added to the
project) drove the real page against two servers:
- uvicorn with `LLM_PROVIDER=offline-heuristic`, for the normal flows;
- uvicorn with no usable provider, for the "unavailable" state.

All 30 checks passed. They cover:
- load, controls and examples;
- submit, status, report and citations showing the evidence lines, plus the Markdown link;
- the inconclusive report;
- a 422 explained in the form;
- `<img onerror>` and `<script>` payloads shown as literal text, with nothing executed or created;
- 2,000-character unbroken text at 1280 px and 390 px;
- the unavailable server (explained, with submit disabled);
- a 500 and a network failure while polling (both explained, with submit re-enabled).

There were no console errors other than the ones the checks deliberately caused. I also reviewed screenshots at desktop
and phone width.

Defects found and fixed, each with a regression test:
1. Long unbroken user text widened the page by about 18,700 px.
2. The evidence table broke words mid-word on phones.
3. The offline stand-in's report was labelled "report by model". It now says "offline-heuristic stand-in (not an AI model)".

The README now states the lack of authentication up front.

Remaining gaps:
- no authentication;
- in-memory jobs (lost on restart, one process only);
- synthetic evidence only;
- a timed-out tool thread still finishes in the background.
