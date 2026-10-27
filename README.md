# Orange Invest AI Agent

FastAPI service for the Orange Invest AI Engineer assignment.

- **Phase 0:** project foundation, configuration, `GET /health`, tests.
- **Phase 1:** typed incident and evidence schemas, plus five deterministic evidence
  tools over local synthetic data. No LLM calls.
- **Phase 2:** a dependency-aware orchestration engine (asyncio): concurrent tasks,
  bounded retries and timeouts, budgets, and partial-completion reporting. No LLM calls.
- **Phase 3:** six collaborating agents (Orchestrator, Observability, Dependency, Reasoning,
  Critic, Reporting) on that engine, using the OpenAI SDK with structured outputs, and a
  bounded critic feedback loop. Tests use a scripted model; no API key is needed to run them.
- **Phase 4:** a labelled 10-case synthetic evaluation set, a single-agent baseline, and an
  evaluation harness comparing both. See [docs/EVALUATION.md](docs/EVALUATION.md).

## Evaluation

```zsh
python -m app.evaluation offline                   # free, deterministic; harness check, NOT a quality measure
python -m app.evaluation live --confirm-billed     # real model calls (billed); not yet run
```

Results go to `evaluation_results/<mode>/` (`report.md`, `results.json`, `per_case.csv`). The dataset and labels are
described in [data/eval/README.md](data/eval/README.md).

**Submission documents:**
- [docs/TECHNICAL_NOTE.md](docs/TECHNICAL_NOTE.md): technical note (≤ 500 words)
- [docs/DEMO.md](docs/DEMO.md): a 3–5 minute demo walkthrough
- [docs/EVALUATION.md](docs/EVALUATION.md): evaluation method and metrics
- [docs/LIVE_RUN_EVAL01_ANALYSIS.md](docs/LIVE_RUN_EVAL01_ANALYSIS.md): the preserved live run

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the layout and the engine lifecycle,
[docs/TOOLS.md](docs/TOOLS.md) for the tool contracts and failure handling, and
[data/README.md](data/README.md) for the fixture data.

## Requirements

- Python 3.12 or newer (developed and tested with 3.13.7)
- macOS or Linux with zsh/bash (primary), or Windows PowerShell (see below)

## Setup (macOS / zsh)

Run from the project root:

```zsh
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies (runtime + test)
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt

# 3. Create your local config from the template (then edit .env if needed)
cp .env.example .env
```

`.env` is git-ignored. Never commit it, and never paste a real key into `.env.example`.
No API key is needed yet.

## Run tests

```zsh
python -m pytest
```

The tests don't depend on your `.env`; they build the app with explicit test settings.

## Run the server

```zsh
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In a second terminal:

```zsh
curl http://127.0.0.1:8000/health
# {"status":"ok","app_name":"Orange Invest AI Agent","version":"0.1.0","environment":"development"}
```

Interactive API docs: http://127.0.0.1:8000/docs

If `.env` contains an invalid value (e.g. `APP_ENV=staging` or `LOG_LEVEL=LOUD`), the server
refuses to start and names the offending setting. Fix the value; there is no silent fallback.

## Use the product: investigate an incident

All evidence is **synthetic**. You investigate against the shared `demo` world or one of the ten evaluation
worlds (`EVAL-01` to `EVAL-10`); there are no real observability integrations. Pick the model in `.env`:

| `LLM_PROVIDER` | What runs |
|---|---|
| `openai` (with `LLM_MODEL`, `LLM_API_KEY`) | The six agents on the real model. Billed; about 100 s per investigation. |
| `offline-heuristic` | A keyless, deterministic rule-based stand-in (most recent change, otherwise most frequent error). **Not an AI model**; every report says so. For demos and local development. |
| unset | The server runs, `/health` works, and submissions return `503` with instructions. |

To try it without a key, without editing `.env`, run
`LLM_PROVIDER=offline-heuristic python -m uvicorn app.main:app --port 8000`.

**Web UI:** open http://127.0.0.1:8000/. Pick an evidence source and an example incident (or type your own),
press *Investigate*, and the report appears when the job finishes.

**API** (interactive docs at `/docs`):

```zsh
curl -s -X POST http://127.0.0.1:8000/investigations -H 'content-type: application/json' -d @examples/incident.json
# 202 {"investigation_id":"inv-…","status":"queued",…}   (Location: /investigations/inv-…)
curl -s http://127.0.0.1:8000/investigations/inv-…            # status: queued | running | completed | failed
curl -s http://127.0.0.1:8000/investigations/inv-…/report     # JSON report (409 until completed)
curl -s http://127.0.0.1:8000/investigations/inv-…/report.md  # readable Markdown report
```

| Route | Purpose |
|---|---|
| `POST /investigations` | Submit an incident. `summary` and timezone-aware `reported_at` are required; `affected_services`, `severity`, `reporter`, `context` and `evidence_source` (default `demo`) are optional. Returns `202`, or `422` (invalid), `429` (too many running; `Retry-After`) or `503` (no model configured). |
| `GET /investigations`, `GET /investigations/{id}` | Job list and status, outcome, model usage. |
| `GET /investigations/{id}/report[?format=markdown]`, `…/report.md` | The report: incident and observed impact, ranked hypotheses with supporting and contradicting evidence (each citation resolved to the record it points to, with its evidence class), facts vs interpretations vs assumptions, the decision with every check, timeline checks, evidence availability, immediate, follow-up and preventive actions, and limitations. `404` unknown, `409` not finished or failed. |
| `GET /evidence-sources`, `GET /capabilities`, `GET /health` | Available evidence worlds with example incidents; whether investigations can run; liveness. |

Jobs run in the background, at most `API_MAX_ACTIVE_INVESTIGATIONS` (default 2) at once. Each job is bounded by the
engine deadline (`INVESTIGATION_DEADLINE_SECONDS`) plus a 60 s hard limit. Jobs are kept in memory (the last 50)
and are lost on restart.

> **Demo limitations.**
> - **No authentication.** Anyone who can reach the port can start investigations (billed, with
>   `LLM_PROVIDER=openai`) and read every report. Keep the server bound to `127.0.0.1` (uvicorn's default), and
>   don't expose it on a network without putting authentication in front of it.
> - **Synthetic evidence only.**
> - **One process, in-memory jobs** (lost on restart).
> - If you reload the web page, it loses track of a running job. Use `GET /investigations` to find it again.

**CLI** (same service and report):

```zsh
python -m app.cli --list                                   # evidence sources and example incidents
python -m app.cli INC-2041                                 # summary of an example incident
python -m app.cli EVAL-07 --format markdown                # full readable report
python -m app.cli --incident-file examples/incident.json   # your own incident (same fields as the API)
```

## Try the evidence tools

With the virtual environment active:

```zsh
python -c "
from app.tools import build_evidence_tools
r = build_evidence_tools()['get_change_history'].run(
    {'start': '2026-10-20T13:30:00Z', 'end': '2026-10-20T15:00:00Z', 'services': ['payments-api']})
print(r.model_dump_json(indent=2))"
```

## Run an investigation with a real model (optional, billed)

The test suite never calls a model. To run the agents for real, put these in `.env`:

```zsh
LLM_PROVIDER=openai
LLM_MODEL=<a model that supports structured outputs via the Responses API>
LLM_API_KEY=<your key>
```

Then:

```zsh
python -m app.cli INC-2041          # summary; --format markdown for the full report, --json for JSON
```

Without a usable model setting, the command exits with code 2 and makes no request.

### Live smoke test

Live integration has been exercised in two billed runs with `gpt-4o-mini`. Both predate the Phase 6 and 7 decision
logic. The first, on 2026-10-26:
- 9 calls, all accepted; the investigation completed in 104 s using about 55k input and 5.7k output tokens.
- The report was inconclusive.

The second, of EVAL-01 through the API, is preserved in [artifacts/live-runs/EVAL-01/](artifacts/live-runs/EVAL-01/)
with its analysis in [docs/LIVE_RUN_EVAL01_ANALYSIS.md](docs/LIVE_RUN_EVAL01_ANALYSIS.md).
Details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). To repeat the controlled smoke test:

1. Create `.env` from `.env.example` and set `LLM_PROVIDER=openai`, `LLM_MODEL` and `LLM_API_KEY`.
   Never paste the key into a terminal command or commit `.env`.
2. Check the configuration without spending anything:
   `python -c "from app.core.config import get_settings; from app.llm import build_llm_client; build_llm_client(get_settings()); print('configured')"`
3. Run once: `python -m app.cli INC-2041`. This is billed. With the default
   `AGENT_MAX_CRITIC_ROUNDS=2` there are at most 8 agent tasks, so 8 model calls on the normal path
   and at most 16 if every agent retries once. The run stops at `INVESTIGATION_DEADLINE_SECONDS`.
4. Record the investigation status, the report conclusion, the per-call outcome/latency/tokens
   table printed at the end, and any `model_*` error codes. Error messages contain only the error
   kind and HTTP status.
5. Do not re-run automatically if it fails. Read the error code first:
   - `model_rejected` (HTTP 400): the schema or model choice was not accepted.
   - HTTP 401/403: the key was rejected.
   - `model_invalid_output`: the model's output failed validation.

## Windows (PowerShell)

These commands mirror the macOS steps but have not been run on Windows.

```powershell
# Use "python" instead of "py -3.12" if the py launcher is unavailable
py -3.12 -m venv .venv

# If activation is blocked, first run: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env

python -m pytest
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# In a second PowerShell window:
Invoke-RestMethod http://127.0.0.1:8000/health
```
