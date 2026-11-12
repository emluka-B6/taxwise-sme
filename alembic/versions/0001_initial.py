"""Initial schema: documents, tax profiles, deadlines, decisions, knowledge base, answers.

Revision ID: 0001
Revises:
Create Date: 2026-11-11 00:00:00

Hand-written for readability, then verified against the ORM metadata with ``alembic
check`` (the autogenerate comparator) on both SQLite and PostgreSQL - an autogenerate run
on top of this revision produces an empty diff. New revisions are expected to be created
with ``alembic revision --autogenerate`` and reviewed/cleaned the same way.

Notes for reviewers
-------------------
* Enum columns use ``native_enum=False`` (VARCHAR + CHECK) so the exact same DDL works on
  SQLite and PostgreSQL and no ``CREATE TYPE`` bookkeeping is needed.
* ``JSONType`` / ``EmbeddingVector`` are imported from ``app.db.types`` on purpose: that is
  the single place where dialect differences (JSONB, TEXT-encoded vectors) are defined.
  Enum *values* below are frozen literals, so changing a Python enum later cannot rewrite
  history.
* Monetary columns are ``NUMERIC(14, 2)``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import EmbeddingVector, JSONType

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    """Portable enum type: VARCHAR + CHECK constraint, no native PG type."""
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
    )


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            _enum(
                "document_type",
                "VAT_INVOICE_SALES",
                "VAT_INVOICE_COST",
                "ZUS",
                "PIT",
                "OTHER",
            ),
            nullable=False,
        ),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("amount_net", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("vat_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("amount_gross", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="PLN", nullable=False),
        sa.Column("counterparty_nip", sa.String(length=10), nullable=True),
        sa.Column(
            "source",
            _enum(
                "document_source",
                "UPLOAD",
                "IMPORT_INFAKT",
                "IMPORT_FAKTUROWNIA",
                "IMPORT_WFIRMA",
                "MANUAL",
            ),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            _enum("document_status", "NEW", "CLASSIFIED", "VERIFIED", "ARCHIVED"),
            server_default="NEW",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("source", "external_id", name="uq_documents_source_external_id"),
    )
    op.create_index("ix_documents_period", "documents", ["period"], unique=False)
    op.create_index("ix_documents_sha256", "documents", ["sha256"], unique=False)

    op.create_table(
        "tax_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "pit_regime",
            _enum("pit_regime", "SCALE", "LINEAR", "LUMP_SUM", "IP_BOX"),
            nullable=False,
        ),
        sa.Column("vat_status", _enum("vat_status", "EXEMPT", "REGISTERED"), nullable=False),
        sa.Column("vat_exemption_threshold_pln", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "zus_variant",
            _enum("zus_variant", "FULL", "PREFERENTIAL_24M", "ULGA_NA_START"),
            nullable=False,
        ),
        sa.Column("ip_box_rate", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_tax_profiles_validity_range"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tax_profiles"),
    )

    op.create_table(
        "deadlines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            _enum(
                "deadline_kind",
                "VAT_DECLARATION",
                "PIT_ADVANCE",
                "ZUS_CONTRIBUTION",
                "CIT_ESTIMATE",
                "OTHER",
            ),
            nullable=False,
        ),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column(
            "status",
            _enum("deadline_status", "PENDING", "DONE", "MISSED", "WAIVED"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_deadlines"),
    )
    op.create_index("ix_deadlines_due_date", "deadlines", ["due_date"], unique=False)
    op.create_index("ix_deadlines_period", "deadlines", ["period"], unique=False)

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("choice", sa.String(length=200), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source_refs", JSONType, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_decisions"),
        sa.UniqueConstraint("topic", "decided_at", name="uq_decisions_topic_decided_at"),
    )

    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "publisher",
            _enum("knowledge_publisher", "MF", "ZUS", "ISAP", "OTHER"),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_sources"),
    )
    op.create_index("ix_knowledge_sources_sha256", "knowledge_sources", ["sha256"], unique=False)

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", EmbeddingVector(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["knowledge_sources.id"],
            name="fk_knowledge_chunks_source_id_knowledge_sources",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_chunks"),
        sa.UniqueConstraint("source_id", "ordinal", name="uq_knowledge_chunks_source_id_ordinal"),
    )

    op.create_table(
        "assistant_answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("citations", JSONType, nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("disclaimer_shown", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assistant_answers"),
    )

    op.create_table(
        "tax_year_params",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tax_year_params"),
        sa.UniqueConstraint("year", "version", name="uq_tax_year_params_year_version"),
    )


def downgrade() -> None:
    op.drop_table("tax_year_params")
    op.drop_table("assistant_answers")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_sources_sha256", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
    op.drop_table("decisions")
    op.drop_index("ix_deadlines_period", table_name="deadlines")
    op.drop_index("ix_deadlines_due_date", table_name="deadlines")
    op.drop_table("deadlines")
    op.drop_table("tax_profiles")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index("ix_documents_period", table_name="documents")
    op.drop_table("documents")
