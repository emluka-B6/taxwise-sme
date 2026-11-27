"""Polish NIP (tax identification number) validation.

The tenth digit is a control digit computed from the first nine with fixed weights. This
helper is shared by the accounting importers (which normalise NIPs to ten digits) and the
e-invoice builders/validators (which require a checksum-valid counterparty), so the two
paths cannot silently disagree on what a valid NIP is.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["NIP_INPUT_RE", "NIP_WEIGHTS", "is_valid_nip"]

#: NIP checksum weights (the tenth digit is the control digit).
NIP_WEIGHTS: Final[tuple[int, ...]] = (6, 5, 7, 2, 3, 4, 5, 6, 7)

#: Every non-digit character is ignored before checksum validation.
NIP_INPUT_RE: Final[re.Pattern[str]] = re.compile(r"\D")


def is_valid_nip(value: str) -> bool:
    """Whether ``value`` (digits and separators allowed) is a checksum-valid Polish NIP."""
    digits = NIP_INPUT_RE.sub("", value)
    if len(digits) != 10 or not digits.isdigit():
        return False
    checksum = sum(
        weight * int(digit) for weight, digit in zip(NIP_WEIGHTS, digits[:9], strict=True)
    )
    control = checksum % 11
    return control != 10 and control == int(digits[9])
