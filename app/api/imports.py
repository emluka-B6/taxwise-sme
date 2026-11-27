"""Invoice imports from accounting platforms: ``POST /api/imports/{provider}``.

The endpoint accepts a multipart CSV export (field ``file``) for one of the
providers mapped in ``config/import_mappings``. Parsing is delegated to
:class:`~app.integrations.accounting.csv_provider.CsvProvider`, which reports a
malformed row as an entry in ``errors[]`` and keeps importing the rest.

Idempotency is enforced on ``(source, external_id)``:

* a new external id -> ``created``;
* an existing id whose data is unchanged -> ``skipped`` (re-import is a no-op);
* an existing id whose data changed -> ``updated`` and the row's status becomes
  ``VERIFIED``, i.e. the conflict requires manual verification.

The response is ``{created, updated, skipped, errors[]}``. Nothing in this
module logs file contents, row values or tokens.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.upload_utils import read_upload
from app.db.session import get_session
from app.integrations.accounting import (
    CsvFormatError,
    MappingConfigError,
    build_csv_provider,
    supported_providers,
)
from app.services import audit
from app.services import imports as imports_service

__all__ = ["MAX_IMPORT_BYTES", "router"]

router = APIRouter(prefix="/api/imports", tags=["imports"])

logger = logging.getLogger(__name__)

#: Hard upload limit; the request is rejected, not truncated.
MAX_IMPORT_BYTES: Final[int] = 10 * 1024 * 1024


class ImportErrorRead(BaseModel):
    """One row rejected by the importer (the rest of the file is still imported)."""

    row: int = Field(ge=1, description="Physical CSV line or API record ordinal.")
    message: str = Field(description="Why the row was rejected.")
    external_id: str | None = Field(
        default=None, description="External id when it could be read from the row."
    )


class ImportResultRead(BaseModel):
    """Outcome of one import run; ``created + updated + skipped`` are valid rows."""

    created: int = Field(ge=0)
    updated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    errors: list[ImportErrorRead] = Field(default_factory=list)


@router.post(
    "/{provider}",
    response_model=ImportResultRead,
    summary="Import invoices from an accounting CSV export",
    responses={
        http_status.HTTP_404_NOT_FOUND: {"description": "Unknown provider"},
        413: {"description": "File above 10 MB"},  # Content Too Large
        422: {"description": "CSV cannot be decoded or lacks required columns"},
    },
)
def import_invoices(
    provider: str,
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[
        UploadFile | None,
        File(description="CSV export of the selected accounting platform, max 10 MB."),
    ] = None,
) -> ImportResultRead:
    """Parse one CSV export, upsert its invoices and report every rejected row."""
    name = provider.strip().lower()
    if name not in supported_providers():
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={
                "message": f"Unknown provider {name!r}.",
                "supported": list(supported_providers()),
            },
        )
    if file is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="A CSV file is required in the 'file' multipart field.",
        )

    data = read_upload(file, max_bytes=MAX_IMPORT_BYTES)
    try:
        csv_provider = build_csv_provider(name, data)
        invoices = csv_provider.fetch_invoices()
    except MappingConfigError as exc:
        logger.error("import mapping unavailable for provider %s", name)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"The import mapping for provider {name!r} is not configured.",
        ) from exc
    except CsvFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    outcome = imports_service.apply_invoices(session, invoices)
    result = ImportResultRead(
        created=outcome.created,
        updated=outcome.updated,
        skipped=outcome.skipped,
        errors=[
            ImportErrorRead(
                row=error.row,
                message=error.message,
                external_id=error.external_id,
            )
            for error in csv_provider.row_errors
        ],
    )
    audit.record(
        session,
        action=audit.AuditAction.DOCUMENTS_IMPORTED,
        entity="import",
        entity_id=name,
        meta={
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors": len(result.errors),
        },
    )
    session.commit()
    logger.info(
        "invoice import: provider=%s created=%d updated=%d skipped=%d errors=%d",
        name,
        result.created,
        result.updated,
        result.skipped,
        len(result.errors),
    )
    return result
