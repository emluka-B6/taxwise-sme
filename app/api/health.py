"""Liveness endpoint.

Deliberately dependency-free: it must answer 200 even when the database is down so that
orchestrators can distinguish "process is alive" from "process is ready".
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.settings import Settings, get_settings

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Response body of ``GET /health``."""

    status: Literal["ok"] = Field(default="ok", description="Static liveness marker.")
    app: str
    version: str
    environment: str


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Return 200 while the process is serving requests."""
    return HealthResponse(
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )
