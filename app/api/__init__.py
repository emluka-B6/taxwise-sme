"""HTTP layer: routers and request/response schemas."""

from app.api.admin import router as admin_router
from app.api.assistant import router as assistant_router
from app.api.calc import router as calc_router
from app.api.deadlines import router as deadlines_router
from app.api.decisions import router as decisions_router
from app.api.documents import router as documents_router
from app.api.exports import router as exports_router
from app.api.health import router as health_router
from app.api.imports import router as imports_router
from app.api.ksef_export import router as ksef_export_router
from app.api.tax_params import router as tax_params_router
from app.api.ui import router as ui_router

__all__ = [
    "admin_router",
    "assistant_router",
    "calc_router",
    "deadlines_router",
    "decisions_router",
    "documents_router",
    "exports_router",
    "health_router",
    "imports_router",
    "ksef_export_router",
    "tax_params_router",
    "ui_router",
]
