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
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.db.session import get_session
from app.domain.enums import DocumentStatus
from app.integrations.accounting import (
    CsvFormatError,
    ImportRowError,
    InvoiceDTO,
    MappingConfigError,
    build_csv_provider,
    supported_providers,
)
from app.services import audit

__all__ = ["MAX_IMPORT_BYTES", "router"]

router = APIRouter(prefix="/api/imports", tags=["imports"])

logger = logging.getLogger(__name__)

#: Hard upload limit; the request is rejected, not truncated.
MAX_IMPORT_BYTES: Final[int] = 10 * 1024 * 1024

_CENT: Final[Decimal] = Decimal("0.01")


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


def _read_upload(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything above the size limit before buffering it."""
    too_large = f"Files larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB are rejected."
    declared = upload.size
    if declared is not None and declared > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=too_large)  # Content Too Large
    data = upload.file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=too_large)  # Content Too Large
    if not data:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty.",
        )
    return data


def _same_money(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left.quantize(_CENT) == right.quantize(_CENT)


def _is_unchanged(row: Document, invoice: InvoiceDTO) -> bool:
    """Whether the stored row already carries exactly the imported invoice data."""
    return (
        row.type == invoice.document_type
        and row.issue_date == invoice.issue_date
        and row.period == invoice.period
        and _same_money(row.amount_net, invoice.amount_net)
        and _same_money(row.vat_amount, invoice.vat_amount)
        and _same_money(row.amount_gross, invoice.amount_gross)
        and row.currency == invoice.currency
        and row.counterparty_nip == invoice.counterparty_nip
    )


def _document_from(invoice: InvoiceDTO) -> Document:
    """New imported row; ``NEW`` marks it as not yet reviewed by the user."""
    return Document(
        type=invoice.document_type,
        issue_date=invoice.issue_date,
        period=invoice.period,
        amount_net=invoice.amount_net,
        vat_amount=invoice.vat_amount,
        amount_gross=invoice.amount_gross,
        currency=invoice.currency,
        counterparty_nip=invoice.counterparty_nip,
        source=invoice.source,
        external_id=invoice.external_id,
        status=DocumentStatus.NEW,
    )


def _apply_invoice(session: Session, invoice: InvoiceDTO) -> str:
    """Upsert one invoice by ``(source, external_id)``; return created/updated/skipped."""
    row = session.scalars(
        select(Document).where(
            Document.source == invoice.source,
            Document.external_id == invoice.external_id,
        )
    ).first()

    if row is None:
        session.add(_document_from(invoice))
        session.flush()
        return "created"

    if _is_unchanged(row, invoice):
        return "skipped"

    # Source data changed: overwrite it and flag the row for manual verification.
    row.type = invoice.document_type
    row.issue_date = invoice.issue_date
    row.period = invoice.period
    row.amount_net = invoice.amount_net
    row.vat_amount = invoice.vat_amount
    row.amount_gross = invoice.amount_gross
    row.currency = invoice.currency
    row.counterparty_nip = invoice.counterparty_nip
    row.status = DocumentStatus.VERIFIED
    session.flush()
    return "updated"


def _apply_invoices(
    session: Session,
    invoices: list[InvoiceDTO],
    row_errors: list[ImportRowError],
) -> ImportResultRead:
    """Apply every valid invoice and turn rejected rows into ``errors[]``."""
    created = updated = skipped = 0
    for invoice in invoices:
        outcome = _apply_invoice(session, invoice)
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1
    return ImportResultRead(
        created=created,
        updated=updated,
        skipped=skipped,
        errors=[
            ImportErrorRead(
                row=error.row,
                message=error.message,
                external_id=error.external_id,
            )
            for error in row_errors
        ],
    )


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

    data = _read_upload(file)
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

    result = _apply_invoices(session, invoices, csv_provider.row_errors)
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
