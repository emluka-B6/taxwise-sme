"""SQLAlchemy 2.0 typed ORM models.

Conventions used across the schema:

* money is always ``NUMERIC(14, 2)`` (``Decimal`` in Python) - never ``float``;
* enumerations are stored as strings with a CHECK constraint (portable across dialects);
* ``created_at`` is timezone-aware and filled by the database;
* tax rates/thresholds are *data* (``tax_year_params.payload``, ``tax_profiles.*``),
  never constants in code.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import EmbeddingVector, JSONType, sa_enum
from app.domain.enums import (
    DeadlineKind,
    DeadlineStatus,
    DocumentSource,
    DocumentStatus,
    DocumentType,
    KnowledgePublisher,
    PitRegime,
    VatStatus,
    ZusVariant,
)

__all__ = [
    "AssistantAnswer",
    "Deadline",
    "Decision",
    "Document",
    "KnowledgeChunk",
    "KnowledgeSource",
    "TaxProfile",
    "TaxYearParams",
]

#: Single source of truth for monetary columns.
MONEY = Numeric(14, 2)


class Document(Base):
    """An accounting document (invoice, ZUS/PIT statement, ...) with its file metadata."""

    __tablename__ = "documents"
    __table_args__ = (
        # Importer idempotency: the same external document cannot be ingested twice.
        UniqueConstraint("source", "external_id", name="uq_documents_source_external_id"),
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_period", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[DocumentType] = mapped_column(
        sa_enum(DocumentType, name="document_type"), nullable=False
    )
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    period: Mapped[str] = mapped_column(
        String(10), nullable=False, doc="Tax period the document belongs to, e.g. '2026-03'."
    )
    amount_net: Mapped[Decimal | None] = mapped_column(
        MONEY, nullable=True, doc="NULL when the document has no net/VAT split (e.g. ZUS)."
    )
    vat_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    amount_gross: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="PLN")
    counterparty_nip: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[DocumentSource] = mapped_column(
        sa_enum(DocumentSource, name="document_source"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        sa_enum(DocumentStatus, name="document_status"),
        nullable=False,
        default=DocumentStatus.NEW,
        server_default=DocumentStatus.NEW.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaxProfile(Base):
    """Versioned tax setup of the taxpayer; the active row is the one covering today."""

    __tablename__ = "tax_profiles"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_tax_profiles_validity_range"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pit_regime: Mapped[PitRegime] = mapped_column(
        sa_enum(PitRegime, name="pit_regime"), nullable=False
    )
    vat_status: Mapped[VatStatus] = mapped_column(
        sa_enum(VatStatus, name="vat_status"), nullable=False
    )
    vat_exemption_threshold_pln: Mapped[Decimal | None] = mapped_column(
        MONEY,
        nullable=True,
        doc="Threshold in force for this profile; seeded from tax_year_params, not from code.",
    )
    zus_variant: Mapped[ZusVariant] = mapped_column(
        sa_enum(ZusVariant, name="zus_variant"), nullable=False
    )
    ip_box_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 4),
        nullable=True,
        doc="Decimal fraction of income taken from tax_year_params; NULL when not on IP_BOX.",
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class Deadline(Base):
    """A filing/payment obligation surfaced by the assistant."""

    __tablename__ = "deadlines"
    __table_args__ = (
        # Idempotency key of the generator: re-running it must not duplicate obligations.
        UniqueConstraint(
            "kind", "period", "due_date", name="uq_deadlines_kind_period_due_date"
        ),
        Index("ix_deadlines_due_date", "due_date"),
        Index("ix_deadlines_period", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[DeadlineKind] = mapped_column(
        sa_enum(DeadlineKind, name="deadline_kind"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[DeadlineStatus] = mapped_column(
        sa_enum(DeadlineStatus, name="deadline_status"),
        nullable=False,
        default=DeadlineStatus.OPEN,
        server_default=DeadlineStatus.OPEN.value,
        doc="Stored state; OVERDUE is derived from due_date in Europe/Warsaw, see services.",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Decision(Base):
    """An auditable tax/accounting decision taken by the user with the assistant's help."""

    __tablename__ = "decisions"
    __table_args__ = (
        # One decision per topic per timestamp - the audit trail must stay append-only.
        UniqueConstraint("topic", "decided_at", name="uq_decisions_topic_decided_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    choice: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_refs: Mapped[list[str] | None] = mapped_column(
        JSONType, nullable=True, doc="List of knowledge source URLs/ids backing the decision."
    )


class KnowledgeSource(Base):
    """Official document (MF/ZUS/ISAP) used as the citation base of assistant answers."""

    __tablename__ = "knowledge_sources"
    __table_args__ = (Index("ix_knowledge_sources_sha256", "sha256"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publisher: Mapped[KnowledgePublisher] = mapped_column(
        sa_enum(KnowledgePublisher, name="knowledge_publisher"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class KnowledgeChunk(Base):
    """Retrieval chunk of a knowledge source; the embedding is a JSON vector in TEXT."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "ordinal", name="uq_knowledge_chunks_source_id_ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingVector(), nullable=True, doc="JSON-serialized vector, no pgvector dependency."
    )

    source: Mapped[KnowledgeSource] = relationship(back_populates="chunks")


class AssistantAnswer(Base):
    """A question/answer pair produced by the assistant, with citations for auditing."""

    __tablename__ = "assistant_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[Any] | None] = mapped_column(
        JSONType,
        nullable=True,
        doc="Citation objects (url, title, published_at, fetched_at, version) or legacy URLs.",
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    disclaimer_shown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )


class TaxYearParams(Base):
    """Versioned, source-linked tax parameters for a year.

    This table is what removes hardcoded rates/thresholds from the codebase: rates,
    thresholds and contribution bases are rows here, sourced from an official URL.
    """

    __tablename__ = "tax_year_params"
    __table_args__ = (UniqueConstraint("year", "version", name="uq_tax_year_params_year_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, doc="Rates/thresholds/bases as data, validated by Pydantic."
    )
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
