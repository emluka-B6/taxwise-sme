"""Period exports: ``GET /api/exports/jpk-pit`` and ``GET /api/exports/tax-pack``.

Both endpoints render files **locally**; nothing is submitted to the Ministry of Finance
(MF). The JPK_PIT response carries ``X-JPK-PIT-Transmission: not-sent`` and its
``Naglowek/SystemInfo`` says the same inside the document.

``GET /api/exports/jpk-pit?year=&month=`` maps the ``VAT_INVOICE_SALES`` /
``VAT_INVOICE_COST`` documents whose ``period`` is that month onto JPK_PIT XML. Before a
file is returned it is structurally validated (required elements, types, row/summary
consistency with the stored documents); findings are reported as ``422`` with the full
violation list. The XSD check uses :mod:`app.integrations.schemas`; because the official
JPK_PIT schema is not configured in ``config/schemas.yaml`` (its existence requires
confirmation at the source), a normal response is marked ``X-Schema-Validation: skipped``.

``GET /api/exports/tax-pack?year=`` builds the annual JPK_PIT plus a CSV statement and a
``manifest.json`` with the SHA-256 and size of every other file, and returns them as a ZIP.
File names contain only the year - never a NIP, a company name or a document id.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.db.session import get_session
from app.domain.enums import DocumentType
from app.integrations import schemas
from app.integrations.jpk import jpk_pit_builder
from app.integrations.jpk.jpk_pit_builder import JpkPitError, JpkPitRow
from app.integrations.schemas import SchemaValidation, ValidationStatus, Violation
from app.services import audit

__all__ = ["router"]

router = APIRouter(prefix="/api/exports", tags=["exports"])

logger = logging.getLogger(__name__)

XML_MEDIA_TYPE: Final[str] = "application/xml"
ZIP_MEDIA_TYPE: Final[str] = "application/zip"

#: Headers that make the "no MF transmission" and schema-status guarantees visible.
TRANSMISSION_HEADER: Final[str] = "X-JPK-PIT-Transmission"
NOT_SENT: Final[str] = "not-sent"
SCHEMA_STATUS_HEADER: Final[str] = "X-Schema-Validation"
SCHEMA_HEADER: Final[str] = "X-JPK-PIT-Schema"
SCHEMA_UNCONFIRMED: Final[str] = "unconfirmed"

#: Document types that make up the statement.
DOCUMENT_TYPES: Final[tuple[DocumentType, ...]] = (
    DocumentType.VAT_INVOICE_SALES,
    DocumentType.VAT_INVOICE_COST,
)

#: Fixed timestamp of ZIP entries: the archive is reproducible byte for byte.
_ZIP_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)


def _schema_path() -> Path | None:
    """Configured JPK_PIT XSD path, or ``None`` when the registry has no such entry.

    The shipped registry carries an *optional* ``jpk_pit`` entry without a verified URL, so
    in practice the path does not exist and :func:`schemas.check` reports ``SKIPPED``.
    """
    try:
        return schemas.get(jpk_pit_builder.JPK_PIT_KEY).path
    except schemas.SchemaConfigError:
        return None


def _headers(status: ValidationStatus) -> dict[str, str]:
    return {
        TRANSMISSION_HEADER: NOT_SENT,
        SCHEMA_STATUS_HEADER: status.value,
        SCHEMA_HEADER: SCHEMA_UNCONFIRMED,
    }


def _unprocessable(
    message: str, violations: list[Violation] | tuple[Violation, ...], status: ValidationStatus
) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": message,
            "violations": [violation.as_dict() for violation in violations],
            "submitted_to_mf": False,
        },
        headers=_headers(status),
    )


def _documents_for(session: Session, year: int, month: int | None) -> tuple[Document, ...]:
    """Revenue/cost documents of a month (``month`` set) or a whole year (``None``)."""
    statement = select(Document).where(Document.type.in_(DOCUMENT_TYPES))
    if month is None:
        statement = statement.where(Document.period.like(f"{year:04d}-%"))
    else:
        statement = statement.where(Document.period == f"{year:04d}-{month:02d}")
    statement = statement.order_by(Document.issue_date, Document.id)
    return tuple(session.scalars(statement).all())


def _build_checked(
    documents: tuple[Document, ...], *, year: int, month: int | None
) -> tuple[bytes, SchemaValidation]:
    """Build a statement, validate it structurally and against an XSD when available."""
    try:
        xml = jpk_pit_builder.build(documents, year=year, month=month)
    except JpkPitError as exc:
        raise _unprocessable(str(exc), exc.violations, ValidationStatus.SKIPPED) from exc

    structural = jpk_pit_builder.validate_jpk_pit(xml, documents=documents)
    if structural:
        raise _unprocessable(
            "The generated JPK_PIT failed structural validation.",
            structural,
            ValidationStatus.SKIPPED,
        )

    try:
        validation = schemas.check(xml, _schema_path())
    except schemas.SchemaError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"The configured JPK_PIT schema cannot be used: {exc}",
        ) from exc
    if validation.status is ValidationStatus.FAILED:
        raise _unprocessable(
            "The generated JPK_PIT does not conform to the configured XSD.",
            validation.violations,
            validation.status,
        )
    return xml, validation


def _statement_csv(rows: tuple[JpkPitRow, ...]) -> bytes:
    """A flat, machine-readable statement: one document per line, UTF-8, ``;`` separated."""
    lines = ["typ;data;nip_kontrahenta;kwota_netto;kwota_vat;kwota_brutto"]
    for row in rows:
        lines.append(
            ";".join(
                [
                    row.typ,
                    row.data.isoformat(),
                    row.nip_kontrahenta,
                    f"{row.netto:.2f}",
                    f"{row.vat:.2f}",
                    f"{row.brutto:.2f}",
                ]
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _manifest(year: int, files: dict[str, bytes], status: ValidationStatus) -> bytes:
    """``manifest.json``: SHA-256 and size of every archived file (not of itself)."""
    payload = {
        "year": year,
        "generated_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "jpk_pit_schema_status": status.value,
        "submitted_to_mf": False,
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for name, data in sorted(files.items())
        ],
    }
    return (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Deterministic ZIP (fixed entry timestamp, sorted names) written to memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(filename=name, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, entries[name])
    return buffer.getvalue()


@router.get(
    "/jpk-pit",
    response_class=Response,
    responses={
        http_status.HTTP_200_OK: {
            "content": {XML_MEDIA_TYPE: {}},
            "description": "JPK_PIT XML generated locally; the file is NOT submitted to MF.",
        },
        http_status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid documents, or the XML failed structural/XSD validation",
        },
    },
    summary="Render a month as JPK_PIT XML (no MF transmission)",
)
def export_jpk_pit(
    session: Annotated[Session, Depends(get_session)],
    year: Annotated[int, Query(ge=2000, le=2100, description="Tax year, e.g. 2026.")],
    month: Annotated[int, Query(ge=1, le=12, description="Tax month, 1-12.")],
) -> Response:
    """Build the JPK_PIT file for ``year``/``month``; never submits anything to MF."""
    documents = _documents_for(session, year, month)
    xml, validation = _build_checked(documents, year=year, month=month)

    headers = _headers(validation.status)
    headers["Content-Disposition"] = f'attachment; filename="jpk_pit_{year}_{month:02d}.xml"'
    audit.record(
        session,
        action=audit.AuditAction.EXPORT_JPK_PIT,
        entity="export",
        entity_id=f"jpk-pit:{year:04d}-{month:02d}",
        meta={
            "kind": "jpk-pit",
            "year": year,
            "month": month,
            "documents": len(documents),
        },
    )
    session.commit()
    logger.info(
        "JPK_PIT XML generated locally, not submitted to MF: year=%s month=%s rows=%d "
        "bytes=%d validation=%s",
        year,
        month,
        len(documents),
        len(xml),
        validation.status.value,
    )
    return Response(content=xml, media_type=XML_MEDIA_TYPE, headers=headers)


@router.get(
    "/tax-pack",
    response_class=Response,
    responses={
        http_status.HTTP_200_OK: {
            "content": {ZIP_MEDIA_TYPE: {}},
            "description": "ZIP with JPK_PIT, a CSV statement and manifest.json checksums.",
        },
        http_status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid documents, or the XML failed structural/XSD validation",
        },
    },
    summary="Yearly tax pack: JPK_PIT + CSV statement + checksum manifest (ZIP)",
)
def export_tax_pack(
    session: Annotated[Session, Depends(get_session)],
    year: Annotated[int, Query(ge=2000, le=2100, description="Tax year, e.g. 2026.")],
) -> Response:
    """Build the yearly archive; the JPK_PIT covers 1 January - 31 December of ``year``."""
    documents = _documents_for(session, year, None)
    xml, validation = _build_checked(documents, year=year, month=None)
    rows = jpk_pit_builder.collect_rows(documents)

    files: dict[str, bytes] = {
        f"jpk_pit_{year}.xml": xml,
        f"statement_{year}.csv": _statement_csv(rows),
    }
    files["manifest.json"] = _manifest(year, files, validation.status)
    archive = _zip_bytes(files)

    headers = _headers(validation.status)
    headers["Content-Disposition"] = f'attachment; filename="tax_pack_{year}.zip"'
    audit.record(
        session,
        action=audit.AuditAction.EXPORT_TAX_PACK,
        entity="export",
        entity_id=f"tax-pack:{year:04d}",
        meta={
            "kind": "tax-pack",
            "year": year,
            "rows": len(rows),
            "documents": len(documents),
            "files": len(files),
        },
    )
    session.commit()
    logger.info(
        "Tax pack generated locally, not submitted to MF: year=%s rows=%d files=%s "
        "bytes=%d validation=%s",
        year,
        len(rows),
        ",".join(sorted(files)),
        len(archive),
        validation.status.value,
    )
    return Response(content=archive, media_type=ZIP_MEDIA_TYPE, headers=headers)
