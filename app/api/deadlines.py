"""Deadline endpoints: the register of filing/payment obligations.

Statuses are reported as ``OPEN``/``DONE``/``OVERDUE``; ``OVERDUE`` is computed from the due
date in Europe/Warsaw (see ``app.services.deadlines.derive_status``), so a late deadline
shows up as overdue without a background job.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.db.models import Deadline as DeadlineRow
from app.db.session import get_session
from app.domain.enums import (
    CURRENT_DEADLINE_STATUSES,
    DeadlineKind,
    DeadlineStatus,
    VatCadence,
)
from app.services import deadlines as deadlines_service
from app.services.tax_params import TaxParamsError

router = APIRouter(prefix="/api/deadlines", tags=["deadlines"])

DeadlineIdPath = Annotated[int, Path(ge=1)]


class DeadlineRead(BaseModel):
    """A deadline as exposed by the API, with its effective status."""

    id: int
    kind: DeadlineKind
    period: str = Field(description="Tax period, e.g. 2026-03, 2026-Q1 or 2026.")
    due_date: date
    status: DeadlineStatus = Field(
        description="OPEN, DONE or OVERDUE; OVERDUE is derived from due_date in Europe/Warsaw."
    )
    notes: str | None = None


class DeadlineUpdate(BaseModel):
    """Partial update of a deadline."""

    model_config = ConfigDict(extra="forbid")

    status: DeadlineStatus | None = Field(
        default=None, description="OPEN, DONE or OVERDUE (legacy values are rejected)."
    )
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        if self.status is None and self.notes is None:
            raise ValueError("provide at least one of: status, notes")
        return self


class DeadlineGenerateRequest(BaseModel):
    """Request body of ``POST /api/deadlines/generate``."""

    model_config = ConfigDict(extra="forbid")

    year: Annotated[int, Field(ge=2000, le=2100)] = Field(
        description="Year whose obligations are generated (periods fall inside it)."
    )
    vat_cadence: VatCadence = Field(
        default=VatCadence.MONTHLY, description="Monthly or quarterly VAT settlement."
    )


class DeadlineGenerateResponse(BaseModel):
    """Outcome of a generation run; re-running it is a no-op (``created`` becomes 0)."""

    year: int
    vat_cadence: VatCadence
    created: int
    existing: int
    deadlines: list[DeadlineRead]


def _read(row: DeadlineRow, *, as_of: date) -> DeadlineRead:
    return DeadlineRead(
        id=row.id,
        kind=row.kind,
        period=row.period,
        due_date=row.due_date,
        status=deadlines_service.derive_status(row.status, row.due_date, as_of=as_of),
        notes=row.notes,
    )


@router.get("", response_model=list[DeadlineRead], summary="List deadlines")
def list_deadlines(
    session: Annotated[Session, Depends(get_session)],
    date_from: Annotated[
        date | None, Query(alias="from", description="Earliest due date.")
    ] = None,
    date_to: Annotated[date | None, Query(alias="to", description="Latest due date.")] = None,
    status: Annotated[DeadlineStatus | None, Query()] = None,
) -> list[DeadlineRead]:
    """Deadlines in due-date order, filtered by due-date range and effective status."""
    if status is not None and status not in CURRENT_DEADLINE_STATUSES:
        raise HTTPException(
            status_code=422,  # Unprocessable Content; avoids the deprecated symbol
            detail=(
                f"status {status.value!r} is read-only legacy data; use one of "
                f"{sorted(member.value for member in CURRENT_DEADLINE_STATUSES)}"
            ),
        )
    today = deadlines_service.today_in_warsaw()
    rows = deadlines_service.list_deadlines(
        session, date_from=date_from, date_to=date_to, status=status, as_of=today
    )
    return [_read(row, as_of=today) for row in rows]


@router.patch("/{deadline_id}", response_model=DeadlineRead, summary="Update a deadline")
def update_deadline(
    deadline_id: DeadlineIdPath,
    payload: DeadlineUpdate,
    session: Annotated[Session, Depends(get_session)],
) -> DeadlineRead:
    """Mark a deadline done/reopened and/or annotate it."""
    if payload.status is not None and payload.status not in CURRENT_DEADLINE_STATUSES:
        raise HTTPException(
            status_code=422,  # Unprocessable Content; avoids the deprecated symbol
            detail=(
                f"status {payload.status.value!r} is read-only legacy data; use one of "
                f"{sorted(member.value for member in CURRENT_DEADLINE_STATUSES)}"
            ),
        )
    row = deadlines_service.mark_deadline(
        session, deadline_id, status=payload.status, notes=payload.notes
    )
    if row is None:
        session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Deadline {deadline_id} does not exist.",
        )
    session.commit()
    return _read(row, as_of=deadlines_service.today_in_warsaw())


@router.post(
    "/generate",
    response_model=DeadlineGenerateResponse,
    summary="Generate a year's deadlines from tax parameters and the active profile",
)
def generate_deadlines(
    payload: DeadlineGenerateRequest,
    session: Annotated[Session, Depends(get_session)],
) -> DeadlineGenerateResponse:
    """Idempotently materialise the obligations of ``year`` (a re-run creates nothing)."""
    profile = deadlines_service.active_profile(session, date(payload.year, 12, 31))
    if profile is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"No tax profile covers {payload.year}-12-31; "
                "create a tax profile before generating deadlines."
            ),
        )
    try:
        result = deadlines_service.generate_deadlines(
            session, payload.year, profile=profile, vat_cadence=payload.vat_cadence
        )
    except TaxParamsError as exc:
        session.rollback()
        raise HTTPException(
            status_code=422,  # Unprocessable Content; avoids the deprecated symbol
            detail=f"cannot generate deadlines for {payload.year}: {exc}",
        ) from exc
    session.commit()
    today = deadlines_service.today_in_warsaw()
    return DeadlineGenerateResponse(
        year=result.year,
        vat_cadence=payload.vat_cadence,
        created=result.created,
        existing=result.existing,
        deadlines=[_read(row, as_of=today) for row in result.deadlines],
    )
