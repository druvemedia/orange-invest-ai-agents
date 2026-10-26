# Orange Invest AI Agent

FastAPI service for the Orange Invest AI Engineer assignment.

- **Phase 0:** project foundation, configuration, `GET /health`, tests.
- **Phase 1:** typed incident and evidence schemas, plus five deterministic evidence
  tools over local synthetic data. No LLM calls.
- **Phase 2:** a dependency-aware orchestration engine (asyncio): concurrent tasks,
  bounded retries and timeouts, budgets, and partial-completion reporting. No LLM calls.

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
