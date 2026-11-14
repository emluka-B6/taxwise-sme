"""ASGI entrypoint: ``uvicorn app.main:app --reload``."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import (
    calc_router,
    deadlines_router,
    decisions_router,
    documents_router,
    health_router,
    tax_params_router,
)
from app.settings import Settings, get_settings

__all__ = ["app", "create_app"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (keeps tests able to build an isolated instance)."""
    resolved = settings or get_settings()
    application = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        debug=resolved.debug,
    )
    application.include_router(health_router)
    application.include_router(tax_params_router)
    application.include_router(calc_router)
    application.include_router(deadlines_router)
    application.include_router(decisions_router)
    application.include_router(documents_router)
    return application


app = create_app()
