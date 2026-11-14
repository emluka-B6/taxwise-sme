"""Domain enumerations.

Persisted members are stored in the database as strings (see ``app.db.types.sa_enum``), so
they are part of the storage contract: rename a member only together with a data migration.
Members marked *not persisted* (``DeadlineRule``, ``VatCadence``) never reach a column; they
describe parameters coming from ``config/tax_params/*.yaml`` and API input.
"""

from enum import StrEnum
from typing import Final

__all__ = [
    "CURRENT_DEADLINE_STATUSES",
    "LEGACY_DEADLINE_STATUS_TO_CURRENT",
    "DeadlineKind",
    "DeadlineRule",
    "DeadlineStatus",
    "DocumentSource",
    "DocumentStatus",
    "DocumentType",
    "KnowledgePublisher",
    "PitRegime",
    "VatCadence",
    "VatStatus",
    "ZusVariant",
]


class DocumentType(StrEnum):
    """Kind of accounting document handled by the assistant."""

    VAT_INVOICE_SALES = "VAT_INVOICE_SALES"
    VAT_INVOICE_COST = "VAT_INVOICE_COST"
    ZUS = "ZUS"
    PIT = "PIT"
    OTHER = "OTHER"


class DocumentSource(StrEnum):
    """Where a document came from."""

    UPLOAD = "UPLOAD"
    IMPORT_INFAKT = "IMPORT_INFAKT"
    IMPORT_FAKTUROWNIA = "IMPORT_FAKTUROWNIA"
    IMPORT_WFIRMA = "IMPORT_WFIRMA"
    MANUAL = "MANUAL"


class DocumentStatus(StrEnum):
    """Lifecycle of a stored document."""

    NEW = "NEW"
    CLASSIFIED = "CLASSIFIED"
    VERIFIED = "VERIFIED"
    ARCHIVED = "ARCHIVED"


class PitRegime(StrEnum):
    """Polish personal income tax settlement regime."""

    SCALE = "SCALE"
    LINEAR = "LINEAR"
    LUMP_SUM = "LUMP_SUM"
    IP_BOX = "IP_BOX"


class VatStatus(StrEnum):
    """VAT registration status of the taxpayer ("zwolniony" vs "czynny podatnik")."""

    EXEMPT = "EXEMPT"
    REGISTERED = "REGISTERED"


class ZusVariant(StrEnum):
    """ZUS contribution variant available to a sole proprietor."""

    FULL = "FULL"
    PREFERENTIAL_24M = "PREFERENTIAL_24M"
    ULGA_NA_START = "ULGA_NA_START"


class DeadlineKind(StrEnum):
    """Type of an obligation tracked by the deadline calendar."""

    VAT_DECLARATION = "VAT_DECLARATION"
    PIT_ADVANCE = "PIT_ADVANCE"
    ZUS_CONTRIBUTION = "ZUS_CONTRIBUTION"
    PIT_ANNUAL = "PIT_ANNUAL"
    CIT_ESTIMATE = "CIT_ESTIMATE"
    OTHER = "OTHER"


class DeadlineStatus(StrEnum):
    """Processing state of a deadline.

    ``OPEN``/``DONE`` are the states the application writes. ``OVERDUE`` is *derived*: an
    ``OPEN`` deadline whose ``due_date`` is before "today" in Europe/Warsaw. The three
    remaining members are historical values kept for storage compatibility (they are
    accepted by the CHECK constraint and normalised on read, see
    ``LEGACY_DEADLINE_STATUS_TO_CURRENT``); new code never writes them.
    """

    OPEN = "OPEN"
    DONE = "DONE"
    OVERDUE = "OVERDUE"
    # --- legacy, read-only -------------------------------------------------
    PENDING = "PENDING"
    MISSED = "MISSED"
    WAIVED = "WAIVED"


#: Statuses that may be written through the API.
CURRENT_DEADLINE_STATUSES: Final[frozenset[DeadlineStatus]] = frozenset(
    {DeadlineStatus.OPEN, DeadlineStatus.DONE, DeadlineStatus.OVERDUE}
)

#: Mapping applied to rows written before OPEN/OVERDUE existed.
LEGACY_DEADLINE_STATUS_TO_CURRENT: Final[dict[DeadlineStatus, DeadlineStatus]] = {
    DeadlineStatus.PENDING: DeadlineStatus.OPEN,
    DeadlineStatus.MISSED: DeadlineStatus.OVERDUE,
    DeadlineStatus.WAIVED: DeadlineStatus.DONE,
}


class DeadlineRule(StrEnum):
    """Deadline rule name used in ``config/tax_params/<year>.yaml`` (*not* persisted).

    A rule only names the obligation; the due date is always ``period end + offset_days``,
    so no filing calendar is baked into Python.
    """

    VAT_MONTHLY = "vat_monthly"
    VAT_QUARTERLY = "vat_quarterly"
    ZUS_DRA = "zus_dra"
    PIT_ADVANCE_MONTHLY = "pit_advance_monthly"
    PIT_ANNUAL = "pit_annual"


class VatCadence(StrEnum):
    """VAT settlement cadence of a taxpayer (*not* persisted yet).

    Passed to ``app.services.deadlines.generate_deadlines`` so that monthly and quarterly
    filers can be modelled without a schema change.
    """

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class KnowledgePublisher(StrEnum):
    """Publisher of an official knowledge source used for citations."""

    MF = "MF"
    ZUS = "ZUS"
    ISAP = "ISAP"
    OTHER = "OTHER"
