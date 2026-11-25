"""Append-only, PII-free audit trail.

Every security-relevant action (upload, status change, export, irreversible retention
purge) leaves one :class:`~app.db.models.AuditLog` row. The table is deliberately boring:
``actor``, ``action``, ``entity``, ``entity_id``, ``at`` and a small ``meta`` JSON object.

Why ``meta`` is allowlisted
---------------------------
An audit trail that stores a client file name, an invoice description or an e-mail address
would itself become a personal-data store. :func:`record` therefore accepts only keys from
:data:`ALLOWED_META_KEYS`, only scalar values, and rejects strings that look like an e-mail
address or a 10/11-digit identifier (NIP/PESEL). Callers pass operational facts - counts,
enum values, years, a cutoff date - never document contents or free-form text.

Nothing in this module logs ``meta``; the only log line carries the action and the entity
identifier (a database id or a fixed label), which are not personal data.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy.orm import Session

from app.db.models import AuditLog

__all__ = [
    "ALLOWED_META_KEYS",
    "DEFAULT_ACTOR",
    "MAX_ENTITY_ID_LENGTH",
    "MAX_META_TEXT_LENGTH",
    "AuditAction",
    "AuditMetadataError",
    "record",
    "sanitize_meta",
]

logger = logging.getLogger(__name__)

#: Actor used for actions performed through the HTTP API (there is no user authentication
#: yet, so a stable, non-identifying label is stored instead of an IP address or user name).
DEFAULT_ACTOR: Final[str] = "api"

#: Longest text value accepted inside ``meta``; longer values are almost always free text.
MAX_META_TEXT_LENGTH: Final[int] = 64

#: ``entity_id`` is a string so that numeric ids, years and composite keys fit.
MAX_ENTITY_ID_LENGTH: Final[int] = 128


class AuditAction(StrEnum):
    """Actions recorded by the application; stored as plain strings."""

    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    DOCUMENT_UPDATED = "DOCUMENT_UPDATED"
    DOCUMENT_STATUS_CHANGED = "DOCUMENT_STATUS_CHANGED"
    DOCUMENTS_IMPORTED = "DOCUMENTS_IMPORTED"
    EXPORT_JPK_PIT = "EXPORT_JPK_PIT"
    EXPORT_TAX_PACK = "EXPORT_TAX_PACK"
    EXPORT_FA3 = "EXPORT_FA3"
    EXPORT_DATA = "EXPORT_DATA"
    RETENTION_PURGE = "RETENTION_PURGE"


#: Keys that may appear in ``AuditLog.meta``. The list is intentionally closed: a caller
#: cannot smuggle a file name, a customer name or a free-form note into the audit trail.
ALLOWED_META_KEYS: Final[frozenset[str]] = frozenset(
    {
        "answers",
        "bytes",
        "candidates",
        "count",
        "created",
        "cutoff",
        "deadlines",
        "decisions",
        "deleted",
        "documents",
        "dry_run",
        "errors",
        "files",
        "from",
        "kind",
        "month",
        "previous_status",
        "previous_type",
        "profiles",
        "retention_years",
        "rows",
        "skipped",
        "source",
        "status",
        "to",
        "type",
        "updated",
        "version",
        "year",
    }
)

#: Strings that must never enter the audit trail, even under an allowlisted key.
_PII_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),  # e-mail address
    re.compile(r"(?<!\d)\d{10,11}(?!\d)"),  # NIP (10) or PESEL (11)
)


class AuditMetadataError(ValueError):
    """``meta`` would contain personal data or a non-scalar value."""


def _clean_text(
    field: str, value: str, *, max_length: int, allow_whitespace: bool = True
) -> str:
    """Reject empty, over-long, control-character or PII-shaped text."""
    text = value.strip()
    if not text:
        raise AuditMetadataError(f"{field} must not be empty")
    if len(text) > max_length:
        raise AuditMetadataError(f"{field} is longer than {max_length} characters")
    if any(ord(char) < 32 for char in text):
        raise AuditMetadataError(f"{field} must not contain control characters")
    if not allow_whitespace and any(char.isspace() for char in text):
        raise AuditMetadataError(f"{field} must be a single machine-readable token")
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            raise AuditMetadataError(f"{field} must not contain personal data")
    return text


def _clean_scalar(key: str, value: object) -> object:
    """Validate one ``meta`` value; only JSON scalars survive."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Counts, years and byte sizes - no unbounded identifiers.
        if not -(10**12) < value < 10**12:
            raise AuditMetadataError(f"meta[{key!r}] is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditMetadataError(f"meta[{key!r}] must be a finite number")
        return value
    if isinstance(value, str):
        return _clean_text(
            f"meta[{key!r}]",
            value,
            max_length=MAX_META_TEXT_LENGTH,
            allow_whitespace=False,
        )
    raise AuditMetadataError(f"meta[{key!r}] must be a scalar value")


def sanitize_meta(meta: Mapping[str, object] | None) -> dict[str, object]:
    """Return a validated copy of ``meta`` or raise :class:`AuditMetadataError`.

    Nested objects, lists and unknown keys are rejected rather than silently dropped: an
    audit entry that cannot be written safely must fail loudly.
    """
    cleaned: dict[str, object] = {}
    for key, value in (meta or {}).items():
        if key not in ALLOWED_META_KEYS:
            raise AuditMetadataError(
                f"meta key {key!r} is not allowlisted; audit metadata must stay PII-free"
            )
        cleaned[key] = _clean_scalar(key, value)
    return cleaned


def record(
    session: Session,
    *,
    action: AuditAction | str,
    entity: str,
    entity_id: str | int,
    actor: str = DEFAULT_ACTOR,
    meta: Mapping[str, object] | None = None,
) -> AuditLog:
    """Append one audit row and flush it so ``id`` is available.

    The caller owns the transaction: the row becomes durable with the caller's
    ``commit()`` and disappears with a rollback. ``meta`` is sanitised first; an unsafe
    value raises :class:`AuditMetadataError` instead of being persisted.
    """
    row = AuditLog(
        actor=_clean_text("actor", actor, max_length=64),
        action=_clean_text("action", str(action), max_length=64),
        entity=_clean_text("entity", entity, max_length=64),
        entity_id=_clean_text(
            "entity_id", str(entity_id), max_length=MAX_ENTITY_ID_LENGTH
        ),
        meta=sanitize_meta(meta),
        at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    # No meta, no entity_id payload: action + entity label only.
    logger.info("audit: action=%s entity=%s", row.action, row.entity)
    return row
