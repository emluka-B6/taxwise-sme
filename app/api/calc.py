"""Calculation preview: ``POST /api/calc/preview``.

The endpoint resolves the requested tax profile and the stored parameters for ``year``,
then calls the pure functions in :mod:`app.services.calc`. It never reads a rate from code:
the response includes ``source_url`` of the exact parameter document used, so a preview can
be traced back to its legal source.

The profile supplies the *regime* (scale, linear, lump sum, IP BOX), the ZUS variant and
the IP BOX eligibility flag (``ip_box_rate`` set). ``inputs`` supplies the amounts. PIT for
the configured regime is required; VAT and ZUS are calculated when their inputs are present.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.models import TaxProfile
from app.db.session import get_session
from app.domain.enums import PitRegime, VatStatus
from app.services import calc
from app.services import tax_params as tax_params_service
from app.services.calc import CalcError, IncompatibleRegimeError
from app.services.tax_params import TaxParams, TaxParamsConfigError

__all__ = [
    "CalcInputs",
    "CalcPreviewRequest",
    "CalcPreviewResponse",
    "CalcResult",
    "VatLineInput",
    "router",
]

router = APIRouter(prefix="/api/calc", tags=["calc"])


class VatLineInput(BaseModel):
    """One VAT line in the request; ``rate`` must match a value in ``vat.rates``."""

    model_config = ConfigDict(extra="forbid")

    net: Decimal = Field(description="Net amount in PLN; negative for a correction.")
    rate: Decimal = Field(description="VAT rate as a decimal fraction, taken from vat.rates.")


class CalcInputs(BaseModel):
    """Amounts supplied by the caller; only the fields relevant to the profile are used."""

    model_config = ConfigDict(extra="forbid")

    income: Decimal | None = Field(
        default=None, description="Annual PIT base for the SCALE and LINEAR regimes."
    )
    revenue: Decimal | None = Field(
        default=None, description="Annual revenue for the LUMP_SUM regime."
    )
    lump_sum_rate: Decimal | None = Field(
        default=None,
        description="Lump-sum rate as a decimal fraction; must be a configured ryczalt rate.",
    )
    qualifying_income: Decimal | None = Field(
        default=None, description="Qualifying income for the IP_BOX regime."
    )
    annual_turnover: Decimal | None = Field(
        default=None,
        description="Annual turnover used to check the VAT exemption threshold.",
    )
    sales: list[VatLineInput] | None = Field(
        default=None, description="Sales lines; an empty list means zero output VAT."
    )
    costs: list[VatLineInput] | None = Field(
        default=None, description="Cost lines; an empty list means zero input VAT."
    )
    month: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="Settlement month for ZUS; omit to skip the contribution calculation.",
    )


class CalcPreviewRequest(BaseModel):
    """Body of ``POST /api/calc/preview``."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2100, description="Tax year whose parameters are used.")
    profile_id: int = Field(ge=1, description="Tax profile that supplies the regimes.")
    inputs: CalcInputs


class CalcResult(BaseModel):
    """Calculated amounts in PLN; ``None`` means the input for that item was not supplied."""

    pit: Decimal | None = Field(default=None, description="PIT for the profile's regime.")
    vat_status: VatStatus | None = Field(
        default=None, description="Status implied by annual_turnover, when supplied."
    )
    vat_due: Decimal | None = Field(
        default=None, description="VAT balance; negative means a credit/carry-forward."
    )
    zus: Decimal | None = Field(default=None, description="Monthly ZUS contributions.")


class CalcPreviewResponse(BaseModel):
    """Result plus the source of the parameter document used for every figure."""

    year: int
    profile_id: int
    result: CalcResult
    source_url: str = Field(description="Official source_url of the stored parameters used.")


def _required(value: Decimal | None, name: str, regime: PitRegime) -> Decimal:
    """Return a required PIT input or explain which regime needs it."""
    if value is None:
        raise calc.CalcInputError(f"{name} is required for the {regime.value} regime")
    return value


def _pit(profile: TaxProfile, params: TaxParams, inputs: CalcInputs) -> Decimal:
    """Dispatch to the pure PIT function selected by the profile's regime."""
    regime = profile.pit_regime
    if regime is PitRegime.SCALE:
        return calc.pit_scale(_required(inputs.income, "income", regime), params)
    if regime is PitRegime.LINEAR:
        return calc.pit_linear(_required(inputs.income, "income", regime), params)
    if regime is PitRegime.LUMP_SUM:
        return calc.pit_lump_sum(
            _required(inputs.revenue, "revenue", regime),
            _required(inputs.lump_sum_rate, "lump_sum_rate", regime),
            params,
        )
    if regime is PitRegime.IP_BOX:
        return calc.pit_ip_box(
            _required(inputs.qualifying_income, "qualifying_income", regime),
            params,
            eligible=profile.ip_box_rate is not None,
        )
    raise calc.CalcInputError(f"unsupported PIT regime {regime.value}")  # pragma: no cover


def _calculate(profile: TaxProfile, params: TaxParams, inputs: CalcInputs) -> CalcResult:
    """Run every calculation whose inputs were supplied."""
    vat_status = (
        None
        if inputs.annual_turnover is None
        else calc.vat_status_check(inputs.annual_turnover, params)
    )
    vat_due = None
    if inputs.sales is not None or inputs.costs is not None:
        sales = [calc.VatLine(line.net, line.rate) for line in (inputs.sales or [])]
        costs = [calc.VatLine(line.net, line.rate) for line in (inputs.costs or [])]
        vat_due = calc.vat_due(sales, costs, params)
    zus = (
        None
        if inputs.month is None
        else calc.zus_contributions(inputs.month, profile.zus_variant, params)
    )
    return CalcResult(
        pit=_pit(profile, params, inputs),
        vat_status=vat_status,
        vat_due=vat_due,
        zus=zus,
    )


def _covers_year(profile: TaxProfile, year: int) -> bool:
    """Whether the profile is in force at the end of ``year`` (annual calculation point)."""
    year_end = date(year, 12, 31)
    return profile.valid_from <= year_end and (
        profile.valid_to is None or profile.valid_to >= year_end
    )


@router.post(
    "/preview",
    response_model=CalcPreviewResponse,
    summary="Preview a tax and contribution calculation",
    responses={
        404: {"description": "Unknown profile or no parameters stored for the year"},
        409: {"description": "Profile does not cover the year or the regime is incompatible"},
        422: {"description": "An input amount, rate, month or variant is not usable"},
    },
)
def preview_calculation(
    payload: CalcPreviewRequest,
    session: Annotated[Session, Depends(get_session)],
) -> CalcPreviewResponse:
    """Calculate PIT/VAT/ZUS for ``payload`` from stored, source-linked parameters."""
    profile = session.get(TaxProfile, payload.profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tax profile {payload.profile_id} does not exist.",
        )
    if not _covers_year(profile, payload.year):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Tax profile {payload.profile_id} is not in force at the end of "
                f"{payload.year}; adjust its validity range or choose another profile."
            ),
        )
    row = tax_params_service.latest(session, payload.year)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No tax parameters stored for {payload.year}. "
                "Run `python scripts/seed_tax_params.py` to load config/tax_params/*.yaml."
            ),
        )
    try:
        params = tax_params_service.from_row(row)
    except TaxParamsConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stored tax parameters for {payload.year} are invalid: {exc}",
        ) from exc
    try:
        result = _calculate(profile, params, payload.inputs)
    except IncompatibleRegimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CalcError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CalcPreviewResponse(
        year=payload.year,
        profile_id=payload.profile_id,
        result=result,
        source_url=str(params.source_url),
    )
