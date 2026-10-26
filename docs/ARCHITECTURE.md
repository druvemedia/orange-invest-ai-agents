# Architecture (Phase 0)

```
app/
  factory.py         create_app(settings=None); importing it has no side effects
  main.py            ASGI entry point: app = create_app(), so bad config fails at startup
  core/              settings (pydantic-settings, SecretStr) and logging
  api/routes/        HTTP routers; one module per resource
  schemas/           Pydantic models: incident report, evidence records,
                     tool queries, EvidenceResult envelope
  tools/             five deterministic evidence tools, fixture loader,
                     fault injection (see docs/TOOLS.md)
  agents/            individual agents                  (Phase 2+)
  orchestration/     multi-agent coordination           (Phase 2+)
  evaluation/        offline evaluation harness         (later phases)
tests/               pytest suite
data/fixtures/       synthetic JSONL evidence shared by all phases (see data/README.md)
docs/                design notes
```

## Conventions

- Configuration comes only from environment variables / `.env`, read through
  `app.core.config.get_settings()`. Routes receive settings via FastAPI `Depends`,
  so tests override them with `app.dependency_overrides`.
- Secrets are `SecretStr`: masked in `repr`/logs and never returned by any endpoint.
- Evidence tools return `EvidenceResult` and never raise for expected failures.
  They report what was observed and how complete it is, not what it means.
- No agent framework is chosen yet.
