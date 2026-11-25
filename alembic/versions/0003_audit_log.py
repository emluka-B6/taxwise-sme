"""Audit trail: the ``audit_log`` table for PII-free action records.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22 00:00:00

Notes for reviewers
-------------------
* Revision ``0002`` is already used by ``0002_deadline_statuses_and_unique_key.py``, so the
  audit table is revision ``0003``; reusing the number would fork the migration chain.
* ``meta`` uses :data:`app.db.types.JSONType` (JSON on SQLite, JSONB on PostgreSQL), the
  same portable type as the other JSON columns, so the DDL renders identically offline.
* ``at`` is timezone-aware with a database-side default; the application also fills it so
  the row is deterministic in tests.
* Indexes support the two access patterns: recent events (``at``) and the history of one
  entity (``entity``, ``entity_id``). There is no personal data in any column by design;
  ``app.services.audit`` rejects non-allowlisted ``meta`` keys and PII-shaped values.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import JSONType

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("meta", JSONType, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"], unique=False)
    op.create_index(
        "ix_audit_log_entity_entity_id", "audit_log", ["entity", "entity_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_entity_entity_id", table_name="audit_log")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")
