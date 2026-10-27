# Demo walkthrough (about 4:30)

Everything below runs **keyless, offline and free** with `LLM_PROVIDER=offline-heuristic`, a rule-based stand-in for
the model ("the most recent change is the cause, otherwise the most frequent error"). Say on camera that it is
**not an AI model**; the page and every report say so too. Real-model behaviour is shown only from the preserved
live runs (step 6). **Never switch to the paid provider during the demo.**

Steps 1–5 and 7 are offline and free. Step 6 shows **recorded** real-model runs; nothing in this demo calls OpenAI.
All outputs quoted below were produced by these exact commands (checked again after the run 4 fixes). Job IDs (`inv-…`) differ on
every run. The CLI writes tool and engine log lines to stderr; the commands below add `2>/dev/null` to hide them.

## Setup (before recording, about 1 minute)

```zsh
cd "<project root>"
source .venv/bin/activate
python -m pytest -q                                   # expect: 581 passed
LLM_PROVIDER=offline-heuristic python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Leave the server running in terminal 1. Open http://127.0.0.1:8000/ in a browser. Keep terminal 2 open in the project
root with the virtual environment active. The env var overrides `.env`, so a configured key is never used.

## Script

| # | Time | On screen | Say (narration) |
|---|---|---|---|
| 1 | 0:00–0:30 | `docs/ARCHITECTURE.md` "Data flow and feedback loop" diagram | "An incident goes to six agents on a bounded engine: Orchestrator, Observability, Dependency, Reasoning, Critic, Reporting. Evidence comes only from five deterministic tools over synthetic data. The model can request queries but never runs them." |
| 2 | 0:30–1:30 | Browser: banner "Running with the offline heuristic stand-in, not an AI model". Source **EVAL-01**, its example, **Investigate** | "Status, then the report: *probable cause identified*. The top hypothesis is the pool-size reduction. Every citation shows the real evidence line and its class: *direct*, *contextual* or *no_change*. The **Decision** section lists every check as PASS or FAIL, and **Timeline checks** show the change and the first symptom in the same 5-minute window, so their order is reported as ambiguous, not guessed." |
| 3 | 1:30–2:15 | Source **EVAL-02** (the reporter blames a deploy), **Investigate** | "The naive rule picks the deploy, but **Timeline checks** say *change follows onset*: the first symptom (database disk usage, 13:35) came 17 minutes before the deploy (13:52), so code caps it to low and the result is **inconclusive**, not confidently wrong. The real cause, a full disk, is only ranked second at low; naming it needs a model." |
| 4 | 2:15–2:50 | Source **EVAL-07** (metrics tool simulated as down), then **EVAL-09** | "EVAL-07: **Evidence availability** shows `ev.metrics` failed. That's a gap, not proof that nothing happened, and the result stays inconclusive. EVAL-09 has no anomaly: *No hypothesis could be grounded*, an honest inconclusive." |
| 5 | 2:50–3:30 | Terminal 2: `python -m app.evaluation offline --out /tmp/orange-demo-eval`, then open `/tmp/orange-demo-eval/report.md` | "The same 10 labelled cases run through the multi-agent system and a single-agent baseline with the same tools and rules: strict 0.4, lenient 0.6, confidently wrong 0.0 for both. These numbers measure the **heuristic stand-in and the harness, not real-model quality**, and don't show that either architecture is better." |
| 6 | 3:30–4:10 | `docs/LIVE_RUN_EVAL01_ANALYSIS.md` (the four-run table), then `artifacts/live-runs/EVAL-01-run4/live_report.md` (Ranked hypotheses, Decision) | "This is **real gpt-4o-mini**, recorded earlier: four billed runs of EVAL-01, single observations, not a benchmark. Every run ranked the pool-size change or its effect first and cited the key evidence, but **none scored correct** against the answer key. Run 3 concluded but named only the mechanism, pool saturation; run 4 named the cause but stayed inconclusive, because the model filed the timeout logs as evidence *against* it. Each run exposed defects we fixed and tested, and each fix is labelled with whether a real model has used it." |
| 7 | 4:10–4:30 | Terminal 2: `python -m pytest -q` | "581 offline tests, no network, no key. Real model calls need an explicit opt-in, `LLM_ALLOW_BILLED_CALLS=true`, and every request is checked against an input-token budget before it is sent. Limits: synthetic evidence only, **no authentication** (keep it on localhost), and jobs live in memory, so they're lost on restart." |

Optional (+30 s), the same flow over the API:

```zsh
curl -s -X POST http://127.0.0.1:8000/investigations -H 'content-type: application/json' -d @examples/incident.json
# 202, JSON with "investigation_id": "inv-…", "status": "queued" and a "links" object; offline it finishes in under a second
curl -s http://127.0.0.1:8000/investigations/<investigation_id>/report.md | head -40
```

`examples/incident.json` is EVAL-02's incident, so this shows the same honest inconclusive result as step 3.

## Expected output (exact, apart from job IDs)

```text
$ LLM_PROVIDER=offline-heuristic python -m app.cli EVAL-01 2>/dev/null
NOTICE: This report was produced by the offline heuristic stand-in (LLM_PROVIDER=offline-heuristic), not by an AI model. …
inv-…: completed (all_tasks_succeeded); source EVAL-01; model offline-heuristic
Conclusion: probable_cause_identified - Reduce database connection pool size (payments-api config_change)
  1. [medium] Reduce database connection pool size (payments-api config_change)  (4 supporting, 0 contradicting)
  2. [low] Errors: Timed out waiting for DB connection from pool after 3000ms  (3 supporting, 0 contradicting)

$ LLM_PROVIDER=offline-heuristic python -m app.cli EVAL-02 2>/dev/null
Conclusion: inconclusive - Update order confirmation email template (orders-service deployment)
  1. [low] Update order confirmation email template (orders-service deployment)  (4 supporting, 0 contradicting)
  2. [low] Errors: Database write failed: could not extend file (No space left on device)  (2 supporting, 0 contradicting)

$ LLM_PROVIDER=offline-heuristic python -m app.cli EVAL-07 2>/dev/null
inv-…: partially_completed (some_tasks_did_not_succeed); source EVAL-07; model offline-heuristic
Conclusion: inconclusive - Lower cache memory limit (redis-cache config_change)
  1. [medium] Lower cache memory limit (redis-cache config_change)  (1 supporting, 0 contradicting)
  …
  evidence ev.metrics: failed (unavailable)

$ python -m app.evaluation offline --out /tmp/orange-demo-eval
  multi_agent   strict 0.4  lenient 0.6  wrong 0.0  failed 0.0
  single_agent  strict 0.4  lenient 0.6  wrong 0.0  failed 0.0
```

## Fallback plan

| Problem | What to do instead |
|---|---|
| Port 8000 in use | Add `--port 8001` and use http://127.0.0.1:8001/. |
| The web page misbehaves | Use the CLI commands above (steps 2–4). Add `--format markdown` for the full report with citations, decision and timeline sections. |
| Server won't start | The CLI doesn't need the server: `LLM_PROVIDER=offline-heuristic python -m app.cli EVAL-02 --format markdown 2>/dev/null`. |
| Anything during step 5 | Open the committed `evaluation_results/offline-heuristic/report.md`; it has the same scores. |
| Tempted to "try it live" | Don't. Live calls are billed (about $0.01–0.02 and 1–1.5 minutes each), need `LLM_ALLOW_BILLED_CALLS=true`, and give a different answer each run. Show the preserved runs instead (step 6). |

Avoid running `python -m app.evaluation offline` without `--out` during the demo: it rewrites the committed results
(only timestamps and latencies change, but the working tree becomes dirty).
