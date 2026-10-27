"""Application factory. Importing this module has no side effects and reads no configuration."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes import health, investigations, ui
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.product.service import InvestigationService
from app.product.sources import load_sources


def build_investigation_service(settings: Settings) -> InvestigationService:
    """Choose the model from settings. Without a usable model, submissions return 503; /health still works."""
    from app.agents import AgentConfig
    from app.evaluation.heuristic_model import HeuristicLLMClient
    from app.llm import BilledCallsNotAllowedError, LLMNotConfiguredError, build_llm_client, check_llm_settings

    limits = dict(max_active=settings.api_max_active_investigations)
    try:
        config = AgentConfig.from_settings(settings)
    except ValueError as exc:
        return InvestigationService(None, model_label="none", agent_config=None, unavailable_reason=str(exc), **limits)
    if settings.llm_provider.strip().lower() == "offline-heuristic":
        return InvestigationService(HeuristicLLMClient, model_label="offline-heuristic", agent_config=config, **limits)
    try:
        check_llm_settings(settings)  # validates settings and the billing opt-in; builds no client
    except BilledCallsNotAllowedError as exc:
        reason = f"{exc} Or set LLM_PROVIDER=offline-heuristic for a keyless demo."
        return InvestigationService(None, model_label="none", agent_config=config, unavailable_reason=reason, **limits)
    except LLMNotConfiguredError as exc:
        reason = (f"{exc} Configure LLM_PROVIDER=openai with LLM_MODEL and LLM_API_KEY, "
                  "or set LLM_PROVIDER=offline-heuristic for a keyless demo.")
        return InvestigationService(None, model_label="none", agent_config=config, unavailable_reason=reason, **limits)
    return InvestigationService(lambda: build_llm_client(settings), model_label=settings.llm_model.strip(),
                                agent_config=config, **limits)


def create_app(settings: Settings | None = None, *, investigation_service: InvestigationService | None = None) -> FastAPI:
    """Build the app. Without ``settings``, configuration is loaded (and validated) from the environment."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    service = investigation_service or build_investigation_service(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await service.shutdown()  # cancel running investigations cleanly on shutdown

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.investigations = service
    app.state.evidence_sources = load_sources()
    app.include_router(health.router)
    app.include_router(investigations.router)
    app.include_router(ui.router)
    return app
