"""Application factory. Importing this module has no side effects and reads no configuration."""

from fastapi import FastAPI

from app import __version__
from app.api.routes import health
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Without ``settings``, configuration is loaded (and validated) from the environment."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version=__version__)
    app.include_router(health.router)
    return app
