"""Invoice upsert logic for accounting-platform imports.

A provider hands over :class:`~app.integrations.accounting.base.InvoiceDTO` rows; this
module decides, per ``(source, external_id)``, whether each one is new, unchanged or
changed and applies it to the ``documents`` table. The field mapping between the DTO and
the row lives in exactly one place, so a new column never has to be edited in three spots.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.domain.enums import DocumentStatus
from app.domain.money import CENT
from app.integrations.accounting.base import InvoiceDTO

__all__ = ["ImportOutcome", "apply_invoices", "document_from_invoice", "is_unchanged"]

#: Money columns compared with grosze precision rather than exact ``Decimal`` equality.
_MONEY_FIELDS: tuple[str, ...] = ("amount_net", "vat_amount", "amount_gross")


def _same_money(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left.quantize(CENT) == right.quantize(CENT)


def apply_fields(row: Document, invoice: InvoiceDTO) -> None:
    """Copy every imported field from ``invoice`` onto ``row``."""
    row.type = invoice.document_type
    row.issue_date = invoice.issue_date
    row.period = invoice.period
    row.amount_net = invoice.amount_net
    row.vat_amount = invoice.vat_amount
    row.amount_gross = invoice.amount_gross
    row.currency = invoice.currency
    row.counterparty_nip = invoice.counterparty_nip


def document_from_invoice(invoice: InvoiceDTO) -> Document:
    """New imported row; ``NEW`` marks it as not yet reviewed by the user."""
    row = Document(
        source=invoice.source,
        external_id=invoice.external_id,
        status=DocumentStatus.NEW,
    )
    apply_fields(row, invoice)
    return row


def is_unchanged(row: Document, invoice: InvoiceDTO) -> bool:
    """Whether ``row`` already carries exactly the imported invoice data."""
    same_plain = (
        row.type == invoice.document_type
        and row.issue_date == invoice.issue_date
        and row.period == invoice.period
        and row.currency == invoice.currency
        and row.counterparty_nip == invoice.counterparty_nip
    )
    same_money = all(
        _same_money(getattr(row, field), getattr(invoice, field)) for field in _MONEY_FIELDS
    )
    return same_plain and same_money


def apply_invoice(session: Session, invoice: InvoiceDTO) -> str:
    """Upsert one invoice by ``(source, external_id)``; return created/updated/skipped."""
    row = session.scalars(
        select(Document).where(
            Document.source == invoice.source,
            Document.external_id == invoice.external_id,
        )
    ).first()

    if row is None:
        session.add(document_from_invoice(invoice))
        session.flush()
        return "created"

    if is_unchanged(row, invoice):
        return "skipped"

    # Source data changed: overwrite it and flag the row for manual verification.
    apply_fields(row, invoice)
    row.status = DocumentStatus.VERIFIED
    session.flush()
    return "updated"


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """Counts of valid rows applied by :func:`apply_invoices`."""

    created: int
    updated: int
    skipped: int


def apply_invoices(session: Session, invoices: list[InvoiceDTO]) -> ImportOutcome:
    """Apply every valid invoice and return the created/updated/skipped counts."""
    created = updated = skipped = 0
    for invoice in invoices:
        outcome = apply_invoice(session, invoice)
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1
    return ImportOutcome(created=created, updated=updated, skipped=skipped)
