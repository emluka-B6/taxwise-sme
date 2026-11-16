"""KSeF integrations: FA(3) invoice XML generation and validation.

Nothing in this package transmits documents to the KSeF API. ``fa3_builder`` renders the
XML, ``fa3_validate`` checks it structurally and :mod:`app.integrations.schemas` validates
it against an XSD when one has been downloaded.
"""

from app.integrations.ksef.fa3_builder import (
    SYSTEM_INFO,
    Fa3Error,
    Fa3Invoice,
    Fa3TotalsMismatch,
    Fa3ValidationError,
    LineItem,
    Party,
    VatRate,
    build,
)
from app.integrations.ksef.fa3_validate import validate_fa3

__all__ = [
    "SYSTEM_INFO",
    "Fa3Error",
    "Fa3Invoice",
    "Fa3TotalsMismatch",
    "Fa3ValidationError",
    "LineItem",
    "Party",
    "VatRate",
    "build",
    "validate_fa3",
]
