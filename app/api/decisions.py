"""Decision log endpoints.

The decision log is the audit trail the assistant must be able to justify: every entry
carries the URLs of the sources it was based on (``source_refs``) and is append-only at the
database level (``UNIQUE(topic, decided_at)`` - a re-post of the same decision conflicts).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Decision
from app.db.session import get_session

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


class DecisionCreate(BaseModel):
    """Payload of ``POST /api/decisions``."""

    model_config = ConfigDict(extra="forbid")

    topic: Annotated[str, Field(min_length=1, max_length=200)]
    choice: Annotated[str, Field(min_length=1, max_length=200)]
    rationale: str | None = Field(default=None, max_length=4000)
    source_refs: list[HttpUrl] = Field(
        default_factory=list,
        description="URLs of the official sources (MF/ZUS/ISAP) backing the decision.",
    )
    decided_at: datetime | None = Field(
        default=None,
        description="Defaults to now (UTC); naive values are interpreted as UTC.",
    )


class DecisionRead(BaseModel):
    """A stored decision."""

    id: int
    decided_at: datetime
    topic: str
    choice: str
    rationale: str | None = None
    source_refs: list[str] = Field(default_factory=list)


def _read(row: Decision) -> DecisionRead:
    return DecisionRead(
        id=row.id,
        decided_at=row.decided_at,
        topic=row.topic,
        choice=row.choice,
        rationale=row.rationale,
        source_refs=list(row.source_refs or ()),
    )


def _normalise(refs: Sequence[HttpUrl]) -> list[str]:
    return [str(ref) for ref in refs]


@router.post(
    "",
    response_model=DecisionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a decision",
    responses={status.HTTP_409_CONFLICT: {"description": "Same topic and timestamp"}},
)
def create_decision(
    payload: DecisionCreate,
    session: Annotated[Session, Depends(get_session)],
) -> DecisionRead:
    """Append a decision together with the sources it was based on."""
    decided_at = payload.decided_at or datetime.now(UTC)
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    row = Decision(
        decided_at=decided_at,
        topic=payload.topic,
        choice=payload.choice,
        rationale=payload.rationale,
        source_refs=_normalise(payload.source_refs),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A decision for this topic and timestamp already exists; "
                "the log is append-only."
            ),
        ) from exc
    session.refresh(row)
    return _read(row)


@router.get("", response_model=list[DecisionRead], summary="List decisions")
def list_decisions(
    session: Annotated[Session, Depends(get_session)],
    topic: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DecisionRead]:
    """Decisions newest first, optionally restricted to one ``topic``."""
    statement = select(Decision).order_by(Decision.decided_at.desc(), Decision.id.desc())
    if topic is not None:
        statement = statement.where(Decision.topic == topic)
    return [_read(row) for row in session.scalars(statement.limit(limit))]
