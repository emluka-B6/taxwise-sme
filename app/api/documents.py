"""Document intake endpoints: secure upload, dedupe and heuristic classification.

``POST /api/documents`` accepts one PDF/XML/CSV file (multipart field ``file``) of at
most 10 MB. Three independent checks run before anything is written:

* the client file name must be a bare name with an allowlisted extension (no traversal);
* the declared MIME type must be one a client uses for that extension;
* the bytes themselves must start like the declared format (``%PDF-``, an XML prolog/root
  tag, or decodable text for CSV).

The file is then stored content-addressed (``<sha256><extension>``, see
``app.services.storage``) and the digest is looked up first: re-uploading the same bytes
returns ``409`` with the id of the existing row instead of creating a duplicate.

Classification runs over the sanitised name and a bounded content prefix
(``app.services.classifier``); ``PATCH /api/documents/{id}`` is the correction path for a
wrong guess. A raw upload carries no parsed amounts yet, so ``issue_date``/``period``
default to the Warsaw date and ``amount_gross`` is stored as ``0.00`` until extraction
fills it in; the row is already ``CLASSIFIED`` because the type is set.

Nothing in this module logs file contents, client file names or storage paths - the only
log line carries the document id and byte size.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final, Self

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.db.session import get_session
from app.domain.enums import DocumentSource, DocumentStatus, DocumentType
from app.services import audit, classifier, storage
from app.services import deadlines as deadlines_service

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "DocumentRead",
    "DocumentUpdate",
    "detect_format",
    "router",
]

router = APIRouter(prefix="/api/documents", tags=["documents"])

logger = logging.getLogger(__name__)

#: Hard upload limit; the request is rejected, not truncated.
MAX_DOCUMENT_BYTES: Final[int] = 10 * 1024 * 1024

#: How much of the file is inspected for magic bytes / text detection.
SNIFF_BYTES: Final[int] = 8192

PDF_MAGIC: Final[bytes] = b"%PDF-"
XML_MAGIC: Final[tuple[bytes, ...]] = (b"<?xml", b"<")
#: UTF-8 BOM and leading whitespace may precede an XML declaration or root element.
XML_LEADING: Final[bytes] = b"\xef\xbb\xbf \t\r\n"

#: MIME types a browser/client may declare for each accepted extension.
CONTENT_TYPES_BY_EXTENSION: Final[dict[str, frozenset[str]]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".xml": frozenset({"application/xml", "text/xml"}),
    ".csv": frozenset({"text/csv", "application/csv", "text/plain"}),
}

DocumentIdPath = Annotated[int, Path(ge=1)]


class DocumentRead(BaseModel):
    """An ingested document; exposes the content-addressed name, never the root path."""

    id: int
    type: DocumentType
    status: DocumentStatus
    source: DocumentSource
    sha256: str
    file_name: str | None = Field(
        default=None,
        description="Stored file name: sha256 + extension (relative to DOCUMENTS_DIR).",
    )
    period: str = Field(description="Tax period of the row, e.g. 2026-03.")
    issue_date: date
    amount_gross: Decimal
    created_at: datetime


class DocumentUpdate(BaseModel):
    """Correction of the heuristic classification and/or lifecycle state."""

    model_config = ConfigDict(extra="forbid")

    type: DocumentType | None = Field(
        default=None, description="Corrected document type (overrides the heuristic)."
    )
    status: DocumentStatus | None = Field(
        default=None, description="NEW, CLASSIFIED, VERIFIED or ARCHIVED."
    )

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        if self.type is None and self.status is None:
            raise ValueError("provide at least one of: type, status")
        return self


def _looks_like_xml(content: bytes) -> bool:
    head = content[:SNIFF_BYTES].lstrip(XML_LEADING)
    return head.startswith(XML_MAGIC)


def _looks_like_text(content: bytes) -> bool:
    """A CSV has no magic bytes: accept files that decode as UTF-8 or Windows-1250."""
    sample = content[:SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    for encoding in ("utf-8", "cp1250"):
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        return True
    return False


def detect_format(content: bytes) -> str | None:
    """Return the extension the bytes actually look like, or ``None`` when unrecognised."""
    if content.startswith(PDF_MAGIC):
        return ".pdf"
    if _looks_like_xml(content):
        return ".xml"
    if _looks_like_text(content):
        return ".csv"
    return None


def _read_upload(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything above the size limit before buffering it."""
    too_large = (
        f"Files larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB are rejected."
    )
    declared = upload.size
    if declared is not None and declared > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail=too_large)  # Content Too Large
    data = upload.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail=too_large)  # Content Too Large
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return data


def _mime_matches(extension: str, declared: str) -> bool:
    """Whether ``declared`` is a plausible MIME type for ``extension``."""
    if declared in CONTENT_TYPES_BY_EXTENSION[extension]:
        return True
    return extension == ".xml" and declared.endswith("+xml")


def _validate_upload(upload: UploadFile, data: bytes) -> tuple[str, str]:
    """Return ``(sanitised_name, extension)`` after name, MIME and magic-byte checks."""
    try:
        filename = storage.sanitize_filename(upload.filename or "")
        extension = storage.extension_for(filename)
    except storage.PathTraversalError as exc:
        raise HTTPException(
            status_code=400,  # Bad Request
            detail="Unsafe file name: path components are not allowed.",
        ) from exc
    except storage.UnsafeFilenameError as exc:
        raise HTTPException(
            status_code=400,  # Bad Request
            detail="Unsafe file name: upload a plain file name.",
        ) from exc
    except storage.UnsupportedFileTypeError as exc:
        allowed = ", ".join(sorted(storage.ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=415,  # Unsupported Media Type
            detail=f"Unsupported file type; allowed extensions: {allowed}.",
        ) from exc

    declared = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if not _mime_matches(extension, declared):
        raise HTTPException(
            status_code=415,  # Unsupported Media Type
            detail=f"Declared content type is not valid for {extension} files.",
        )

    detected = detect_format(data)
    if detected is None:
        raise HTTPException(
            status_code=415,  # Unsupported Media Type
            detail="File content does not match a supported document format.",
        )
    if detected != extension:
        raise HTTPException(
            status_code=415,  # Unsupported Media Type
            detail=f"File content does not match the {extension} extension.",
        )
    return filename, extension


def _read(row: Document) -> DocumentRead:
    return DocumentRead(
        id=row.id,
        type=row.type,
        status=row.status,
        source=row.source,
        sha256=row.sha256 or "",
        file_name=row.file_path,
        period=row.period,
        issue_date=row.issue_date,
        amount_gross=row.amount_gross,
        created_at=row.created_at,
    )


@router.post(
    "",
    response_model=DocumentRead,
    status_code=http_status.HTTP_201_CREATED,
    summary="Upload a financial document",
    responses={
        http_status.HTTP_409_CONFLICT: {"description": "Content already stored (same sha256)"},
        413: {"description": "File exceeds 10 MB"},  # Content Too Large
        415: {"description": "Extension, MIME type or magic bytes rejected"},
    },
)
def upload_document(
    file: Annotated[UploadFile, File(description="PDF, XML or CSV file, max 10 MB.")],
    session: Annotated[Session, Depends(get_session)],
) -> DocumentRead:
    """Validate, deduplicate, classify and store one uploaded document."""
    data = _read_upload(file)
    filename, extension = _validate_upload(file, data)
    digest = hashlib.sha256(data).hexdigest()

    existing = session.scalars(select(Document).where(Document.sha256 == digest)).first()
    if existing is not None:
        existing_id = existing.id  # capture before rollback expires the instance
        session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "message": "This document content is already stored.",
                "existing_id": existing_id,
            },
        )

    try:
        stored = storage.save_document(data, sha256=digest, extension=extension)
    except (storage.StorageError, OSError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The document could not be stored.",
        ) from exc

    today = deadlines_service.today_in_warsaw()
    row = Document(
        type=classifier.classify(filename, data),
        issue_date=today,
        period=f"{today:%Y-%m}",
        # Amounts are extracted later; a raw upload has no parsed value yet.
        amount_gross=Decimal("0.00"),
        source=DocumentSource.UPLOAD,
        file_path=stored.name,
        sha256=digest,
        status=DocumentStatus.CLASSIFIED,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        action=audit.AuditAction.DOCUMENT_UPLOADED,
        entity="document",
        entity_id=row.id,
        meta={
            "type": row.type.value,
            "status": row.status.value,
            "source": row.source.value,
            "bytes": len(data),
        },
    )
    session.commit()
    session.refresh(row)
    logger.info("document ingested: id=%s bytes=%d", row.id, len(data))
    return _read(row)


@router.patch(
    "/{document_id}",
    response_model=DocumentRead,
    summary="Correct a document's type or status",
)
def update_document(
    document_id: DocumentIdPath,
    payload: DocumentUpdate,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentRead:
    """Override the heuristic classification and/or move the document along its lifecycle."""
    row = session.get(Document, document_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} does not exist.",
        )
    previous_type = row.type
    previous_status = row.status
    if payload.type is not None:
        row.type = payload.type
    if payload.status is not None:
        row.status = payload.status
    status_changed = row.status != previous_status
    audit.record(
        session,
        action=(
            audit.AuditAction.DOCUMENT_STATUS_CHANGED
            if status_changed
            else audit.AuditAction.DOCUMENT_UPDATED
        ),
        entity="document",
        entity_id=row.id,
        meta={
            "previous_type": previous_type.value,
            "type": row.type.value,
            "previous_status": previous_status.value,
            "status": row.status.value,
        },
    )
    session.commit()
    session.refresh(row)
    return _read(row)
