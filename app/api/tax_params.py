"""Tax parameter endpoints.

``GET /api/tax-params/{year}`` returns the stored, validated document - including
``source_url`` and ``effective_from`` - so every figure the assistant uses can be traced to
an official source and to the version that was in force.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services import tax_params as tax_params_service
from app.services.tax_params import TaxParams

router = APIRouter(prefix="/api/tax-params", tags=["tax-params"])

YearPath = Annotated[int, Path(ge=2000, le=2100, description="Tax year, e.g. 2026.")]


@router.get(
    "/{year}",
    response_model=TaxParams,
    summary="Tax parameters for a year",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No parameters stored for the year"}},
)
def get_tax_params(
    year: YearPath,
    session: Annotated[Session, Depends(get_session)],
) -> TaxParams:
    """Return the highest-version parameters stored for ``year``."""
    row = tax_params_service.latest(session, year)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No tax parameters stored for {year}. "
                "Run `python scripts/seed_tax_params.py` to load config/tax_params/*.yaml."
            ),
        )
    return tax_params_service.from_row(row)
