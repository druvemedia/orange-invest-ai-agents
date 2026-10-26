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
python -m app.cli INC-2041          # summary; add --json for the full state and report
```

Without these settings the command exits with code 2 and makes no request.

### Live smoke test

Live integration was verified once on 2026-10-26 with `gpt-4o-mini`:
- 9 calls, all accepted; the investigation completed in 104 s using about 55k input and 5.7k output tokens.
- The report was inconclusive.

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
