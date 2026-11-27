"""FA(3) format metadata shared by the builder and the validator.

``VatRate`` and ``RATE_SLOTS`` map the supported ``P_12`` codes onto the ``P_13_x`` /
``P_14_x`` summary elements. They live here (not in ``fa3_builder``) so the validator
depends on the format, not on the builder.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

__all__ = ["RATE_SLOTS", "RateSlot", "VatRate"]


class VatRate(StrEnum):
    """FA(3) ``P_12`` codes supported by the builder (23/8/5/0 percent and exemption)."""

    R23 = "23"
    R8 = "8"
    R5 = "5"
    R0 = "0"
    ZW = "zw"

    @property
    def percent(self) -> Decimal | None:
        """The rate as a percentage, or ``None`` for the exempt (``zw``) code."""
        return None if self is VatRate.ZW else Decimal(self.value)


@dataclass(frozen=True, slots=True)
class RateSlot:
    """Where a rate's summary belongs in ``Fa`` (``P_13_x`` / ``P_14_x`` / ``P_12``)."""

    p12: str
    net_element: str
    vat_element: str | None


#: The FA(3) mapping of the supported rates. ``P_12`` is a code, not a fraction: the
#: zero-rate code is the domestic one (``0 KR``) and exemption carries no VAT element.
RATE_SLOTS: Final[dict[VatRate, RateSlot]] = {
    VatRate.R23: RateSlot("23", "P_13_1", "P_14_1"),
    VatRate.R8: RateSlot("8", "P_13_2", "P_14_2"),
    VatRate.R5: RateSlot("5", "P_13_3", "P_14_3"),
    VatRate.R0: RateSlot("0 KR", "P_13_4", "P_14_4"),
    VatRate.ZW: RateSlot("zw", "P_13_7", None),
}
