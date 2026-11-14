"""Deadline statuses OPEN/OVERDUE, the PIT_ANNUAL kind and the generator's idempotency key.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12 00:00:00

Notes for reviewers
-------------------
* Enum values are *added*, never removed: ``PENDING``/``MISSED``/``WAIVED`` stay in the
  CHECK constraint so that rows written by earlier releases keep loading. The service layer
  normalises them (``LEGACY_DEADLINE_STATUS_TO_CURRENT``) and only ever writes
  ``OPEN``/``DONE``/``OVERDUE``.
* ``UNIQUE(kind, period, due_date)`` is the idempotency key of the deadline generator: a
  second run for the same year must not create duplicates.
* The table is recreated from an explicitly declared ``copy_from`` table (no reflection) so
  the revision renders deterministic DDL in offline mode too (``alembic upgrade --sql``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEADLINE_KINDS_V1: tuple[str, ...] = (
    "VAT_DECLARATION",
    "PIT_ADVANCE",
    "ZUS_CONTRIBUTION",
    "CIT_ESTIMATE",
    "OTHER",
)
DEADLINE_KINDS_V2: tuple[str, ...] = (
    "VAT_DECLARATION",
    "PIT_ADVANCE",
    "ZUS_CONTRIBUTION",
    "PIT_ANNUAL",
    "CIT_ESTIMATE",
    "OTHER",
)
DEADLINE_STATUSES_V1: tuple[str, ...] = ("PENDING", "DONE", "MISSED", "WAIVED")
DEADLINE_STATUSES_V2: tuple[str, ...] = (
    "OPEN",
    "DONE",
    "OVERDUE",
    "PENDING",
    "MISSED",
    "WAIVED",
)

UNIQUE_KEY_NAME = "uq_deadlines_kind_period_due_date"


def _enum(name: str, *values: str) -> sa.Enum:
    """Portable enum type: VARCHAR + CHECK constraint, no native PG type."""
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
    )


def _deadlines_table(
    *, kinds: tuple[str, ...], statuses: tuple[str, ...], unique_key: bool = False
) -> sa.Table:
    """Declared shape of ``deadlines``; passed to ``batch_alter_table`` as ``copy_from``.

    ``copy_from`` describes the table *as it exists at that point of the revision chain*
    (V1 before the upgrade, V2 before the downgrade), which keeps the emitted DDL
    deterministic and reflection-free.
    """
    constraints: list[sa.Constraint] = [sa.PrimaryKeyConstraint("id", name="pk_deadlines")]
    if unique_key:
        constraints.append(
            sa.UniqueConstraint("kind", "period", "due_date", name=UNIQUE_KEY_NAME)
        )
    return sa.Table(
        "deadlines",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", _enum("deadline_kind", *kinds), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column(
            "status",
            _enum("deadline_status", *statuses),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        *constraints,
        sa.Index("ix_deadlines_due_date", "due_date"),
        sa.Index("ix_deadlines_period", "period"),
    )


def upgrade() -> None:
    with op.batch_alter_table(
        "deadlines",
        copy_from=_deadlines_table(kinds=DEADLINE_KINDS_V1, statuses=DEADLINE_STATUSES_V1),
        recreate="always",
    ) as batch_op:
        batch_op.alter_column(
            "kind",
            type_=_enum("deadline_kind", *DEADLINE_KINDS_V2),
            existing_type=_enum("deadline_kind", *DEADLINE_KINDS_V1),
        )
        batch_op.alter_column(
            "status",
            type_=_enum("deadline_status", *DEADLINE_STATUSES_V2),
            existing_type=_enum("deadline_status", *DEADLINE_STATUSES_V1),
            existing_server_default="PENDING",
            server_default="OPEN",
        )
        batch_op.create_unique_constraint(
            UNIQUE_KEY_NAME, ["kind", "period", "due_date"]
        )


def downgrade() -> None:
    # Rows using the new values cannot survive a downgrade; map them onto their legacy
    # equivalents first so the narrowed CHECK constraint holds.
    op.execute(
        "UPDATE deadlines SET status = 'PENDING' WHERE status = 'OPEN' "
        "OR status = 'MISSED' OR status = 'WAIVED'"
    )
    op.execute("UPDATE deadlines SET status = 'MISSED' WHERE status = 'OVERDUE'")
    op.execute(
        "UPDATE deadlines SET kind = 'VAT_DECLARATION' WHERE kind = 'PIT_ANNUAL'"
    )

    with op.batch_alter_table(
        "deadlines",
        copy_from=_deadlines_table(
            kinds=DEADLINE_KINDS_V2, statuses=DEADLINE_STATUSES_V2, unique_key=True
        ),
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(UNIQUE_KEY_NAME, type_="unique")
        batch_op.alter_column(
            "kind",
            type_=_enum("deadline_kind", *DEADLINE_KINDS_V1),
            existing_type=_enum("deadline_kind", *DEADLINE_KINDS_V2),
        )
        batch_op.alter_column(
            "status",
            type_=_enum("deadline_status", *DEADLINE_STATUSES_V1),
            existing_type=_enum("deadline_status", *DEADLINE_STATUSES_V2),
            existing_server_default="OPEN",
            server_default="PENDING",
        )
