"""Domain enumerations.

Values are persisted in the database as strings (see ``app.db.types.sa_enum``), so they
are part of the storage contract: rename a member only together with a data migration.
"""

from enum import StrEnum

__all__ = [
    "DeadlineKind",
    "DeadlineStatus",
    "DocumentSource",
    "DocumentStatus",
    "DocumentType",
    "KnowledgePublisher",
    "PitRegime",
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
    CIT_ESTIMATE = "CIT_ESTIMATE"
    OTHER = "OTHER"


class DeadlineStatus(StrEnum):
    """Processing state of a deadline."""

    PENDING = "PENDING"
    DONE = "DONE"
    MISSED = "MISSED"
    WAIVED = "WAIVED"


class KnowledgePublisher(StrEnum):
    """Publisher of an official knowledge source used for citations."""

    MF = "MF"
    ZUS = "ZUS"
    ISAP = "ISAP"
    OTHER = "OTHER"
