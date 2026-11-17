"""JPK_PIT integrations: revenue/cost statement generation and validation.

Nothing in this package transmits documents to the Ministry of Finance (MF) systems.
``jpk_pit_builder`` maps stored revenue/cost documents of a period to JPK_PIT XML, checks
it structurally and :mod:`app.integrations.schemas` validates it against an XSD when one
has been configured and downloaded.
"""

from app.integrations.jpk.jpk_pit_builder import (
    SYSTEM_INFO,
    JpkPitError,
    JpkPitRow,
    JpkPitSummary,
    JpkPitTotalsMismatch,
    JpkPitValidationError,
    build,
    collect_rows,
    period_bounds,
    summarize,
    validate_jpk_pit,
)

__all__ = [
    "SYSTEM_INFO",
    "JpkPitError",
    "JpkPitRow",
    "JpkPitSummary",
    "JpkPitTotalsMismatch",
    "JpkPitValidationError",
    "build",
    "collect_rows",
    "period_bounds",
    "summarize",
    "validate_jpk_pit",
]
