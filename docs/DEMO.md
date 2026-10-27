# Demo walkthrough (3–5 minutes)

Recorded entirely offline with `LLM_PROVIDER=offline-heuristic`: no key, no cost. Say on camera that this mode is
a rule-based stand-in, **not an AI model**; the page and every report say so too. Real-model behaviour is shown
from the preserved live run (step 6), not re-run.

**Before recording**

```zsh
source .venv/bin/activate
LLM_PROVIDER=offline-heuristic python -m uvicorn app.main:app --port 8000   # leave running
```

| # | Time | Show | Point to make |
|---|---|---|---|
| 1 | 0:00–0:30 | `README.md` "Use the product" section and `docs/ARCHITECTURE.md` "Data flow and feedback loop" diagram | Six agents on a bounded engine; evidence from five deterministic tools over synthetic data; the critic loop. |
| 2 | 0:30–1:30 | http://127.0.0.1:8000/: pick **EVAL-01** and its example, then *Investigate* | Job status, then the report. Ranked hypotheses; each citation shows the actual evidence line and its class (direct / contextual / no_change). The **Decision** section lists every check as PASS/FAIL. **Timeline checks** report "ordering ambiguous" (the change and the first pool-saturation sample fall in the same 5-minute interval). |
| 3 | 1:30–2:15 | Same page: source **EVAL-02** (a deploy blamed by the reporter) | The deploy came after the first "No space left on device" symptom. Timeline checks say "change follows onset … cannot explain the onset", and the conclusion stays **inconclusive** instead of blaming the deploy. Mention that the offline rule cannot find the real cause (disk full); that requires a model. |
| 4 | 2:15–2:45 | Source **EVAL-07** (metrics tool simulated as down), then **EVAL-09** (no anomaly) | EVAL-07: "Evidence availability" lists `ev.metrics` as failed (a gap, not proof of absence). EVAL-09: "No hypothesis could be grounded"; an honest inconclusive. |
| 5 | 2:45–3:30 | Terminal: `python -m app.evaluation offline`, then open `evaluation_results/offline-heuristic/report.md` | Same 10 cases for the multi-agent system and the single-agent baseline; metric table and per-case outcomes. Read the caveat aloud: offline scores test the harness, not model quality. |
| 6 | 3:30–4:15 | `artifacts/live-runs/EVAL-01/live_report.md` and `docs/LIVE_RUN_EVAL01_ANALYSIS.md` | The one real gpt-4o-mini run of EVAL-01: correct cause ranked first, but inconclusive, plus the defects it exposed (flat metric cited as "increased load", topology as support, false "metrics unavailable"). Each defect maps to a code fix tested offline. One observation, not a benchmark. |
| 7 | 4:15–4:45 | Terminal: `python -m pytest -q` (about 5 s) | 462 offline tests, no network or API key. Close on limitations: synthetic evidence, no auth, the new rules not yet validated with a real model. |

Optional (+30 s): `curl -s -X POST http://127.0.0.1:8000/investigations -H 'content-type: application/json'
-d @examples/incident.json` and then `GET /investigations/<id>/report.md`. This shows the same flow over the API.
