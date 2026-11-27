"""Money and decimal coercion shared by the calculation engine and the e-invoice builders.

The "grosz" rounding convention (``ROUND_HALF_UP`` to two decimal places) is identical
across every module that handles PLN amounts, so it lives here rather than being
re-implemented per integration. ``coerce_decimal`` is the single input-coercion helper:
the only thing that varies between callers is the domain error type raised on bad input.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

__all__ = ["CENT", "ZERO", "coerce_decimal", "money"]

#: Monetary precision: one grosz.
CENT: Final[Decimal] = Decimal("0.01")

#: Zero as a two-decimal money amount (used in summaries and comparisons).
ZERO: Final[Decimal] = Decimal("0.00")


def coerce_decimal(value: object, field: str, *, error: type[Exception]) -> Decimal:
    """Convert ``value`` to a finite ``Decimal`` without changing its scale.

    Floats go through their string form so binary floating-point noise never enters a
    calculation. ``error`` is the exception type raised on invalid input (the caller's
    domain error, e.g. ``CalcInputError`` or ``Fa3Error``).
    """
    if isinstance(value, bool) or value is None:
        raise error(f"{field} must be a decimal number, got {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise error(f"{field} must be finite, got {value!r}")
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise error(f"{field} is not a valid decimal number: {value!r}") from exc
    else:
        raise error(f"{field} must be a decimal number, got {type(value).__name__}")
    if not result.is_finite():
        raise error(f"{field} must be finite, got {value!r}")
    return result


def money(value: Decimal) -> Decimal:
    """Round ``value`` to grosze with ``ROUND_HALF_UP`` (the calc-engine convention)."""
    return value.quantize(CENT, ROUND_HALF_UP)
