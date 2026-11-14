"""HTTP layer: routers and request/response schemas."""

from app.api.deadlines import router as deadlines_router
from app.api.decisions import router as decisions_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.tax_params import router as tax_params_router

__all__ = [
    "deadlines_router",
    "decisions_router",
    "documents_router",
    "health_router",
    "tax_params_router",
]
