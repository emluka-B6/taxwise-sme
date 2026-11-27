"""Deterministic tax and contribution arithmetic for a Polish sole proprietor.

Every rate, threshold and contribution base comes from :class:`TaxYearParams` (validated
from ``config/tax_params/<year>.yaml``); this module contains only the *formulas*. The
public functions are pure: same inputs and same parameter document always produce the same
result, with no database, clock or environment access.

Money is :class:`Decimal` and every public result is rounded to grosze with
``ROUND_HALF_UP``. Input amounts may be ``Decimal``, ``int``, ``str`` or ``float``; floats
are converted through their string form so binary floating-point noise never enters the
calculation. A non-positive income/revenue/base means "nothing to tax" and yields zero,
while an unknown rate, variant or month raises :class:`CalcInputError`. Using the IP BOX
formula without the eligibility flag raises :class:`IncompatibleRegimeError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.domain.enums import VatStatus, ZusVariant
from app.domain.money import coerce_decimal, money
from app.services.tax_params import TaxParams, ZusComponent

__all__ = [
    "CalcError",
    "CalcInputError",
    "IncompatibleRegimeError",
    "VatLine",
    "pit_ip_box",
    "pit_linear",
    "pit_lump_sum",
    "pit_scale",
    "vat_due",
    "vat_status_check",
    "zus_contributions",
]

_ZERO: Final[Decimal] = Decimal("0.00")
_ZERO_EXACT: Final[Decimal] = Decimal("0")
_MIN_MONTH: Final[int] = 1
_MAX_MONTH: Final[int] = 12


class CalcError(Exception):
    """Base class for calculation failures."""


class CalcInputError(CalcError, ValueError):
    """An amount, rate, month or variant is missing or not usable."""


class IncompatibleRegimeError(CalcError, ValueError):
    """The requested tax regime is not available for this taxpayer or parameter document."""


@dataclass(frozen=True, slots=True)
class VatLine:
    """One VAT line: a net amount in PLN and a rate expressed as a decimal fraction.

    ``net`` may be negative (a correction/credit note); ``rate`` must be one of
    ``params.vat.rates`` and is checked by :func:`vat_due`, which is where the parameter
    document is available. The constructor still accepts strings/ints so JSON input can be
    passed through without a manual ``Decimal`` conversion.
    """

    net: Decimal
    rate: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "net", _money(self.net, "vat line net"))
        object.__setattr__(self, "rate", _decimal(self.rate, "vat line rate"))


#: A VAT entry may be a :class:`VatLine`, a ``{"net": ..., "rate": ...}`` mapping or a
#: two-element ``(net, rate)`` sequence.
type VatEntry = VatLine | Mapping[str, object] | Sequence[object]


def _decimal(value: object, field: str) -> Decimal:
    """Convert ``value`` to a finite ``Decimal`` without changing its scale."""
    return coerce_decimal(value, field, error=CalcInputError)


def _money(value: object, field: str) -> Decimal:
    """Convert ``value`` to PLN rounded to grosze (``ROUND_HALF_UP``)."""
    return money(coerce_decimal(value, field, error=CalcInputError))


def _non_negative_money(value: object, field: str) -> Decimal:
    """Convert a tax base to PLN; a non-positive base becomes zero (nothing to tax)."""
    amount = _money(value, field)
    return amount if amount > 0 else _ZERO


def _rate(value: object, field: str, *, allowed: Sequence[Decimal] | None = None) -> Decimal:
    """Validate a decimal-fraction rate and, optionally, membership in ``allowed``."""
    rate = _decimal(value, field)
    if rate < 0 or rate > 1:
        raise CalcInputError(f"{field} must be a fraction between zero and one, got {rate}")
    if allowed is not None and rate not in allowed:
        raise CalcInputError(
            f"{field} {rate} is not one of the rates configured for this year "
            f"({', '.join(str(candidate) for candidate in allowed)})"
        )
    return rate


def _round(value: Decimal) -> Decimal:
    """Round a monetary value to grosze with ``ROUND_HALF_UP``."""
    return money(value)


def pit_scale(income: object, params: TaxParams) -> Decimal:
    """Progressive PIT on the annual scale.

    The brackets and the tax-free amount come from ``params.pit.scale``. The tax-free
    amount reduces the tax due (it is not a separate allowance deducted from income), which
    is why income inside the tax-free band pays nothing and the first bracket is taxed
    progressively above it.

    Args:
        income: annual taxable income in PLN; zero or negative means no tax.
        params: validated parameters for the tax year.

    Returns:
        Tax in PLN rounded to grosze (never negative).
    """
    base = _non_negative_money(income, "income")
    if base == _ZERO:
        return _ZERO

    tax = _ZERO_EXACT
    lower = _ZERO_EXACT
    for bracket in params.pit.scale.rates:
        upper = bracket.threshold if bracket.threshold is not None else base
        taxable_in_bracket = min(base, upper) - lower
        if taxable_in_bracket > 0:
            tax += taxable_in_bracket * bracket.rate
        if bracket.threshold is None or base <= bracket.threshold:
            break
        lower = bracket.threshold

    first_bracket = params.pit.scale.rates[0]
    tax_free_base = params.pit.scale.tax_free_amount
    if first_bracket.threshold is not None:
        tax_free_base = min(tax_free_base, first_bracket.threshold)
    tax_free_credit = tax_free_base * first_bracket.rate
    return _round(max(_ZERO_EXACT, tax - tax_free_credit))


def pit_linear(income: object, params: TaxParams) -> Decimal:
    """Flat-rate PIT on the annual taxable income (``params.pit.linear.rate``).

    Args:
        income: annual taxable income in PLN; zero or negative means no tax.
        params: validated parameters for the tax year.

    Returns:
        Tax in PLN rounded to grosze (never negative).
    """
    base = _non_negative_money(income, "income")
    return _round(base * params.pit.linear.rate)


def pit_lump_sum(revenue: object, rate: object, params: TaxParams) -> Decimal:
    """Lump-sum PIT ("ryczalt") on registered revenue at one of the configured rates.

    Args:
        revenue: annual registered revenue in PLN; zero or negative means no tax.
        rate: decimal-fraction rate; must be one of ``params.pit.lump_sum.rates_by_pkd``.
        params: validated parameters for the tax year (the legal rate list).

    Returns:
        Tax in PLN rounded to grosze (never negative).

    Raises:
        CalcInputError: when ``rate`` is not a configured lump-sum rate.
    """
    allowed = tuple(params.pit.lump_sum.rates_by_pkd.values())
    checked_rate = _rate(rate, "lump-sum rate", allowed=allowed)
    base = _non_negative_money(revenue, "revenue")
    return _round(base * checked_rate)


def pit_ip_box(
    qualifying_income: object, params: TaxParams, eligible: bool = False
) -> Decimal:
    """IP BOX PIT on qualifying intellectual-property income.

    Args:
        qualifying_income: qualifying income in PLN; zero or negative means no tax.
        params: validated parameters for the tax year (the IP BOX rate lives there).
        eligible: the taxpayer's eligibility flag. It comes from the tax profile
            (``ip_box_rate`` is set) rather than from the yearly parameter document, so it
            must be supplied explicitly.

    Returns:
        Tax in PLN rounded to grosze (never negative).

    Raises:
        IncompatibleRegimeError: when ``eligible`` is false - IP BOX cannot be applied
            without documented eligibility.
    """
    if not eligible:
        raise IncompatibleRegimeError(
            "IP BOX is only available to an eligible taxpayer; "
            "the profile has no IP BOX eligibility flag"
        )
    base = _non_negative_money(qualifying_income, "qualifying income")
    return _round(base * params.pit.ip_box.rate)


def vat_status_check(annual_turnover: object, params: TaxParams) -> VatStatus:
    """Return the VAT status implied by annual turnover and the exemption threshold.

    The threshold is inclusive: turnover exactly equal to
    ``params.vat.exemption_threshold_pln`` still allows the exemption. Turnover above it
    requires registration. A non-positive turnover is treated as zero.

    Args:
        annual_turnover: annual turnover in PLN.
        params: validated parameters for the tax year.

    Returns:
        :attr:`VatStatus.EXEMPT` at or below the threshold, otherwise
        :attr:`VatStatus.REGISTERED`.
    """
    turnover = _non_negative_money(annual_turnover, "annual turnover")
    threshold = params.vat.exemption_threshold_pln
    return VatStatus.EXEMPT if turnover <= threshold else VatStatus.REGISTERED


def _coerce_vat_line(entry: VatEntry) -> VatLine:
    """Normalise one VAT entry to :class:`VatLine`."""
    if isinstance(entry, VatLine):
        return entry
    if isinstance(entry, Mapping):
        if "net" not in entry or "rate" not in entry:
            raise CalcInputError("a VAT line mapping must contain 'net' and 'rate'")
        return VatLine(
            _money(entry["net"], "vat line net"),
            _decimal(entry["rate"], "vat line rate"),
        )
    if (
        isinstance(entry, Sequence)
        and not isinstance(entry, (str, bytes))
        and len(entry) == 2
    ):
        return VatLine(
            _money(entry[0], "vat line net"),
            _decimal(entry[1], "vat line rate"),
        )
    raise CalcInputError(
        "a VAT entry must be VatLine, {'net': ..., 'rate': ...} or a (net, rate) pair"
    )


def _vat_total(entries: Sequence[VatEntry], params: TaxParams, label: str) -> Decimal:
    """Sum VAT for ``entries``, rounding each line to grosze like an invoice would."""
    total = _ZERO_EXACT
    for index, entry in enumerate(entries):
        line = _coerce_vat_line(entry)
        rate = _rate(line.rate, f"{label}[{index}].rate", allowed=params.vat.rates)
        total += _round(line.net * rate)
    return total


def vat_due(
    sales: Sequence[VatEntry], costs: Sequence[VatEntry], params: TaxParams
) -> Decimal:
    """VAT payable for a period: output VAT on sales minus input VAT on costs.

    Each line's VAT is ``net x rate`` rounded to grosze and must use a rate from
    ``params.vat.rates``. Negative ``net`` values are allowed for corrections. A negative
    result means input VAT exceeds output VAT (a credit/carry-forward), which is returned
    as-is so callers can distinguish it from a zero liability.

    Args:
        sales: taxable sales lines (net, rate).
        costs: deductible cost lines (net, rate).
        params: validated parameters for the tax year.

    Returns:
        VAT balance in PLN rounded to grosze; negative means a credit.

    Raises:
        CalcInputError: when a line is malformed or uses a rate not configured for the year.
    """
    output_vat = _vat_total(sales, params, "sales")
    input_vat = _vat_total(costs, params, "costs")
    return _round(output_vat - input_vat)


def _zus_components(variant: ZusVariant, params: TaxParams) -> Sequence[ZusComponent]:
    """Return the configured components of ``variant`` or fail with a domain error."""
    if variant is ZusVariant.FULL:
        return params.zus.components
    spec = params.zus.variants.get(variant)
    if spec is None:
        raise CalcInputError(
            f"ZUS variant {variant.value} is not configured for {params.year} "
            f"v{params.version}"
        )
    return spec.components


def _resolve_zus_variant(variant: ZusVariant | str) -> ZusVariant:
    """Accept an enum member or its string value."""
    if isinstance(variant, ZusVariant):
        return variant
    try:
        return ZusVariant(variant)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in ZusVariant)
        raise CalcInputError(
            f"unknown ZUS variant {variant!r}; expected one of {allowed}"
        ) from exc


def zus_contributions(month: int, variant: ZusVariant | str, params: TaxParams) -> Decimal:
    """Monthly ZUS contributions (social insurance, Fundusz Pracy, FGSP and health).

    Components are read from ``params.zus`` (``FULL``) or ``params.zus.variants``
    (preferential / ulga na start). Each component is ``base x rate`` rounded to grosze and
    the rounded components are then summed, matching how ZUS presents a monthly settlement.

    ``month`` identifies the settlement month (1-12). Monthly amounts are constant within a
    variant, so the value does not vary with the calendar month: the caller supplies the
    variant in force for that month. In particular, "ulga na start" is configured as the
    component set payable during its six-month window (health contribution only); once the
    window ends the profile's variant changes and so does the amount.

    Args:
        month: settlement month, ``1`` through ``12``.
        variant: :class:`ZusVariant` member or its string value.
        params: validated parameters for the tax year.

    Returns:
        Total monthly contribution in PLN rounded to grosze.

    Raises:
        CalcInputError: for an invalid month or a variant not configured for the year.
    """
    if isinstance(month, bool) or not isinstance(month, int):
        raise CalcInputError(f"month must be an integer from {_MIN_MONTH} to {_MAX_MONTH}")
    if not _MIN_MONTH <= month <= _MAX_MONTH:
        raise CalcInputError(f"month must be from {_MIN_MONTH} to {_MAX_MONTH}, got {month}")
    resolved = _resolve_zus_variant(variant)
    components = _zus_components(resolved, params)
    total = sum(
        (_round(component.base * component.rate) for component in components),
        _ZERO_EXACT,
    )
    return _round(total)

