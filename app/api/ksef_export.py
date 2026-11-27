"""KSeF FA(3) export: ``GET /api/documents/{id}/ksef-xml?validate=true|false``.

The endpoint renders the XML locally and returns it as ``application/xml``. **Nothing is
transmitted to KSeF** - the file is meant for review, signing and upload by an authorised
tool; the response says so in the ``X-KSeF-Transmission: not-sent`` header and inside the
document (``Naglowek/SystemInfo``).

The stored ``documents`` row supplies the type, issue date, currency and the net/VAT/gross
amounts that the XML must add up to. Everything a sales invoice needs beyond those
aggregates (number, seller, buyer, line items, exemption basis) is supplied in the request
body, because the skeleton does not parse invoices into line items yet. The request stays
read-only, hence ``GET``.

Marking:

* ``X-KSeF-Schema-Version`` - the FA(3) version from ``config/schemas.yaml``;
* ``X-Schema-Validation`` - ``ok`` / ``failed`` / ``skipped`` (no XSD downloaded).

Errors: ``404`` unknown document, ``409`` not a sales invoice, ``422`` invalid invoice data
or an XML that fails structural/XSD validation (always with the full violation list),
``500`` unusable (corrupt) schema file.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.export_utils import unprocessable
from app.db.models import Document
from app.db.session import get_session
from app.domain.enums import DocumentType
from app.integrations import schemas
from app.integrations.ksef import fa3_builder, fa3_validate
from app.integrations.ksef.fa3_builder import Fa3Error, Fa3Invoice
from app.integrations.schemas import ValidationStatus, Violation
from app.services import audit

__all__ = ["Fa3InvoicePayload", "LineItemPayload", "PartyPayload", "router"]

router = APIRouter(prefix="/api/documents", tags=["ksef"])

logger = logging.getLogger(__name__)

DocumentIdPath = Annotated[int, Path(ge=1)]

#: Media type of the FA(3) document itself.
XML_MEDIA_TYPE: Final[str] = "application/xml"

#: Header that makes the "no transmission" guarantee visible on every response.
TRANSMISSION_HEADER: Final[str] = "X-KSeF-Transmission"
NOT_SENT: Final[str] = "not-sent"


class PartyPayload(BaseModel):
    """Seller/buyer data as sent by the caller."""

    model_config = ConfigDict(extra="forbid")

    nip: str = Field(description="Polish NIP; a checksum is verified by the builder.")
    name: str = Field(description="Full legal name of the party.")
    address_line_1: str = Field(description="Street and number (FA(3) AdresL1).")
    address_line_2: str | None = Field(
        default=None, description="Postal code and city (FA(3) AdresL2)."
    )
    country_code: str = Field(default="PL", description="ISO 3166-1 alpha-2 country code.")
    jst: str = Field(default="2", description='Public-entity flag: "1" yes, "2" no.')
    gv: str = Field(default="2", description='VAT-group member flag: "1" yes, "2" no.')


class LineItemPayload(BaseModel):
    """One invoice line (FA(3) ``FaWiersz``)."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="Goods/service name (FA(3) P_7).")
    quantity: Decimal = Field(gt=0, description="Quantity (P_8B); must be positive.")
    unit: str = Field(default="szt.", description="Unit of measure (P_8A).")
    unit_net_price: Decimal = Field(ge=0, description="Unit net price (P_9A).")
    vat_rate: str = Field(
        description='VAT rate code (P_12): "23", "8", "5", "0" or "zw" (exempt).'
    )


class Fa3InvoicePayload(BaseModel):
    """Invoice data that is not stored on the ``documents`` row."""

    model_config = ConfigDict(extra="forbid")

    number: str = Field(description="Invoice number (P_2).")
    seller: PartyPayload
    buyer: PartyPayload
    lines: list[LineItemPayload] = Field(min_length=1, description="At least one line.")
    sale_date: date | None = Field(default=None, description="Date of sale/delivery (P_6).")
    place: str | None = Field(default=None, description="Place of issue (P_1M).")
    currency: str = Field(default="PLN", description="ISO 4217 code (KodWaluty).")
    exemption_basis: str | None = Field(
        default=None, description="Legal basis written to P_19A for 'zw' lines."
    )
    created_at: datetime | None = Field(
        default=None,
        description="DataWytworzeniaFa; defaults to now (UTC), useful when replaying a document.",
    )

    def to_invoice(self) -> Fa3Invoice:
        """Convert the request into the builder's immutable input model."""
        return Fa3Invoice(
            number=self.number,
            seller=fa3_builder.Party(
                nip=self.seller.nip,
                name=self.seller.name,
                address_line_1=self.seller.address_line_1,
                address_line_2=self.seller.address_line_2,
                country_code=self.seller.country_code,
                jst=self.seller.jst,
                gv=self.seller.gv,
            ),
            buyer=fa3_builder.Party(
                nip=self.buyer.nip,
                name=self.buyer.name,
                address_line_1=self.buyer.address_line_1,
                address_line_2=self.buyer.address_line_2,
                country_code=self.buyer.country_code,
                jst=self.buyer.jst,
                gv=self.buyer.gv,
            ),
            lines=tuple(
                fa3_builder.LineItem(
                    description=line.description,
                    quantity=line.quantity,
                    unit=line.unit,
                    unit_net_price=line.unit_net_price,
                    vat_rate=line.vat_rate,
                )
                for line in self.lines
            ),
            sale_date=self.sale_date,
            place=self.place,
            currency=self.currency,
            exemption_basis=self.exemption_basis,
        )


def _schema_spec() -> schemas.SchemaSpec:
    """Registry entry used for validation (patched in tests to point at a fixture XSD)."""
    return schemas.get(schemas.FA3_KEY)


def _headers(version: str, status: ValidationStatus) -> dict[str, str]:
    return {
        "X-KSeF-Schema-Version": version,
        "X-Schema-Validation": status.value,
        TRANSMISSION_HEADER: NOT_SENT,
    }


def _unprocessable(
    message: str,
    violations: Sequence[Violation],
    *,
    version: str,
    status: ValidationStatus,
) -> HTTPException:
    return unprocessable(
        message,
        violations,
        headers=_headers(version, status),
        not_submitted_key="sent_to_ksef",
    )


@router.get(
    "/{document_id}/ksef-xml",
    response_class=Response,
    responses={
        http_status.HTTP_200_OK: {
            "content": {XML_MEDIA_TYPE: {}},
            "description": "FA(3) XML generated locally; the file is NOT sent to KSeF.",
        },
        http_status.HTTP_409_CONFLICT: {"description": "Document is not a sales invoice"},
        http_status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid invoice data, or the XML failed validation (violation list)"
        },
    },
    summary="Render a document as KSeF FA(3) XML (no transmission)",
)
def export_ksef_xml(
    document_id: DocumentIdPath,
    payload: Fa3InvoicePayload,
    session: Annotated[Session, Depends(get_session)],
    validate: Annotated[
        bool,
        Query(description="Validate the XML against the FA(3) XSD when it has been downloaded."),
    ] = False,
) -> Response:
    """Build the FA(3) file for ``document_id``; never uploads anything to KSeF."""
    row = session.get(Document, document_id)
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} does not exist.",
        )
    if row.type is not DocumentType.VAT_INVOICE_SALES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"FA(3) sales invoices require a VAT_INVOICE_SALES document, got {row.type}.",
        )

    fmt = schemas.fa3_format()
    try:
        xml = fa3_builder.build(row, payload.to_invoice(), created_at=payload.created_at)
    except Fa3Error as exc:
        raise _unprocessable(
            str(exc),
            exc.violations,
            version=fmt.version,
            status=ValidationStatus.SKIPPED,
        ) from exc

    structural = fa3_validate.validate_fa3(xml, fmt=fmt)
    if structural:
        raise _unprocessable(
            "The generated XML failed structural FA(3) validation.",
            structural,
            version=fmt.version,
            status=ValidationStatus.SKIPPED,
        )

    validation = schemas.SchemaValidation(status=ValidationStatus.SKIPPED)
    if validate:
        try:
            validation = schemas.check(xml, _schema_spec().path)
        except schemas.SchemaError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"The configured FA(3) schema cannot be used: {exc}",
            ) from exc
        if validation.status is ValidationStatus.FAILED:
            raise _unprocessable(
                "The generated XML does not conform to the configured FA(3) XSD.",
                validation.violations,
                version=fmt.version,
                status=validation.status,
            )

    filename = _filename(payload.number)
    headers = _headers(fmt.version, validation.status)
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    audit.record(
        session,
        action=audit.AuditAction.EXPORT_FA3,
        entity="document",
        entity_id=row.id,
        meta={
            "type": row.type.value,
            "version": fmt.version,
            "status": validation.status.value,
        },
    )
    session.commit()
    logger.info(
        "FA(3) XML generated locally, not sent to KSeF: document_id=%s bytes=%d validation=%s",
        row.id,
        len(xml),
        validation.status.value,
    )
    return Response(content=xml, media_type=XML_MEDIA_TYPE, headers=headers)


def _filename(number: str) -> str:
    """A header-safe attachment name derived from the invoice number."""
    safe = "".join(char if char.isalnum() or char in "-_. " else "_" for char in number.strip())
    return f"{safe or 'faktura'}.xml"

