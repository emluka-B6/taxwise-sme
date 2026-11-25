"""Time-based retention with an explicitly confirmed, irreversible purge.

Policy
------
A :class:`~app.db.models.Document` becomes a *retention candidate* when its ``created_at``
is older than ``RETENTION_YEARS`` (default **5**) years. The cutoff is a calendar
subtraction from the current UTC timestamp, so leap days do not shift the boundary.

Safety model
------------
* :func:`build_plan` and :func:`run_retention` with ``dry_run=True`` are **read-only**:
  they return the candidate list and write neither rows nor an audit entry.
* A destructive run requires the token configured in
  ``RETENTION_CONFIRMATION_TOKEN``; a missing/empty/invalid token raises
  :class:`RetentionConfirmationError` before anything is touched.
* Files are content-addressed (``<sha256><extension>``) and are unlinked before the rows
  are deleted, only when no retained document still references the same path. Paths are
  re-validated against ``DOCUMENTS_DIR``.
* The ``RETENTION_PURGE`` audit row (counts, cutoff, retention window - no personal data)
  is flushed **before** the first unlink and committed with the row deletions, so a purge
  cannot start without a pending audit entry.

The job is intentionally **not** scheduled: an operator starts it through
``POST /api/admin/retention/run`` or a script, after reviewing a dry run.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.services import audit, storage
from app.settings import get_settings

__all__ = [
    "RETENTION_YEARS",
    "RetentionCandidate",
    "RetentionConfirmationError",
    "RetentionError",
    "RetentionPlan",
    "RetentionResult",
    "build_plan",
    "cutoff_for",
    "run_retention",
]

logger = logging.getLogger(__name__)

#: Default retention window in years; ``RETENTION_YEARS`` overrides it at runtime.
RETENTION_YEARS: Final[int] = 5

#: Entity label used in the audit trail (one row per destructive run).
_AUDIT_ENTITY: Final[str] = "document"


class RetentionError(Exception):
    """Base class for retention failures; callers translate these to HTTP errors."""


class RetentionConfirmationError(RetentionError):
    """The destructive run was not confirmed with the configured token."""


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    """A document that is older than the retention window."""

    id: int
    document_type: str
    source: str
    period: str
    created_at: datetime
    file_name: str | None


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """Result of a read-only retention scan."""

    retention_years: int
    cutoff: datetime
    candidates: tuple[RetentionCandidate, ...]

    @property
    def count(self) -> int:
        """Number of expired documents."""
        return len(self.candidates)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Outcome of a dry run or of an irreversible purge."""

    dry_run: bool
    retention_years: int
    cutoff: datetime
    candidates: tuple[RetentionCandidate, ...]
    deleted: int
    files_deleted: int
    audit_id: int | None


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC (SQLite returns naive values)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _years_before(moment: datetime, years: int) -> datetime:
    """Calendar subtraction; 29 February becomes 28 February in a non-leap year."""
    try:
        return moment.replace(year=moment.year - years)
    except ValueError:
        return moment.replace(year=moment.year - years, day=28)


def cutoff_for(retention_years: int, *, now: datetime | None = None) -> datetime:
    """Return the oldest ``created_at`` a retained document may have."""
    if retention_years < 1:
        raise ValueError("retention_years must be at least 1")
    moment = _as_utc(now) if now is not None else datetime.now(UTC)
    return _years_before(moment, retention_years)


def _resolve_years(retention_years: int | None) -> int:
    years = get_settings().retention_years if retention_years is None else retention_years
    if years < 1:
        raise ValueError("retention_years must be at least 1")
    return years


def _expired_rows(session: Session, cutoff: datetime) -> list[Document]:
    """Documents whose ``created_at`` is strictly before ``cutoff`` (oldest first)."""
    statement = (
        select(Document)
        .where(Document.created_at < cutoff)
        .order_by(Document.created_at, Document.id)
    )
    return list(session.scalars(statement).all())


def _candidate(row: Document) -> RetentionCandidate:
    return RetentionCandidate(
        id=row.id,
        document_type=row.type.value,
        source=row.source.value,
        period=row.period,
        created_at=_as_utc(row.created_at),
        file_name=row.file_path,
    )


def build_plan(
    session: Session,
    *,
    retention_years: int | None = None,
    now: datetime | None = None,
) -> RetentionPlan:
    """Scan the database and return expired documents **without changing anything**."""
    years = _resolve_years(retention_years)
    cutoff = cutoff_for(years, now=now)
    rows = _expired_rows(session, cutoff)
    return RetentionPlan(
        retention_years=years,
        cutoff=cutoff,
        candidates=tuple(_candidate(row) for row in rows),
    )


def _configured_token() -> str:
    token = get_settings().retention_confirmation_token
    return token.get_secret_value() if token is not None else ""


def _require_confirmation(confirmation_token: str | None) -> None:
    """Refuse a destructive run unless the ENV-configured token matches."""
    expected = _configured_token()
    if not expected:
        raise RetentionConfirmationError(
            "RETENTION_CONFIRMATION_TOKEN is not configured; the purge is disabled"
        )
    provided = confirmation_token or ""
    if not provided or not secrets.compare_digest(
        expected.encode("utf-8"), provided.encode("utf-8")
    ):
        raise RetentionConfirmationError("invalid retention confirmation token")


def _files_to_delete(session: Session, rows: list[Document]) -> list[Path]:
    """Resolve files of ``rows`` to unlink, skipping those a retained row still uses."""
    relative_paths = {row.file_path for row in rows if row.file_path}
    if not relative_paths:
        return []

    candidate_ids = [row.id for row in rows]
    still_referenced = set(
        session.scalars(
            select(Document.file_path).where(
                Document.file_path.in_(relative_paths),
                ~Document.id.in_(candidate_ids),
            )
        ).all()
    )

    root = storage.documents_root()
    paths: list[Path] = []
    for relative in sorted(relative_paths):
        if relative in still_referenced:
            # A retained row shares the content-addressed file; never break it.
            logger.warning("retention: keeping a file that a retained document still uses")
            continue
        try:
            path: Path = storage.ensure_within(root / relative, root)
        except storage.StorageError as exc:
            raise RetentionError(
                "a stored document path escapes DOCUMENTS_DIR; purge aborted"
            ) from exc
        if path.is_file():
            paths.append(path)
    return paths


def _unlink_files(paths: list[Path]) -> int:
    """Unlink the planned files; a file that vanished meanwhile is not an error."""
    deleted = 0
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RetentionError("a stored document file could not be deleted") from exc
        deleted += 1
    return deleted


def run_retention(
    session: Session,
    *,
    dry_run: bool = True,
    confirmation_token: str | None = None,
    retention_years: int | None = None,
    now: datetime | None = None,
    actor: str = audit.DEFAULT_ACTOR,
) -> RetentionResult:
    """Scan for expired documents and, when confirmed, delete their files and rows.

    ``dry_run=True`` is the default and never writes. A destructive run requires the
    token from ``RETENTION_CONFIRMATION_TOKEN`` and appends exactly one audit entry.
    The purge commits before returning; the caller must not reuse the deleted objects.
    """
    years = _resolve_years(retention_years)
    cutoff = cutoff_for(years, now=now)
    rows = _expired_rows(session, cutoff)
    candidates = tuple(_candidate(row) for row in rows)

    if dry_run:
        logger.info(
            "retention dry-run: candidates=%d retention_years=%d cutoff=%s",
            len(candidates),
            years,
            cutoff.date().isoformat(),
        )
        return RetentionResult(
            dry_run=True,
            retention_years=years,
            cutoff=cutoff,
            candidates=candidates,
            deleted=0,
            files_deleted=0,
            audit_id=None,
        )

    _require_confirmation(confirmation_token)

    # Resolve and validate every path first, then flush the audit row: no file is
    # unlinked unless a pending audit entry exists in the same transaction.
    files = _files_to_delete(session, rows)
    entry = audit.record(
        session,
        actor=actor,
        action=audit.AuditAction.RETENTION_PURGE,
        entity=_AUDIT_ENTITY,
        entity_id=cutoff.date().isoformat(),
        meta={
            "retention_years": years,
            "cutoff": cutoff.date().isoformat(),
            "candidates": len(candidates),
            "deleted": len(rows),
            "files": len(files),
            "dry_run": False,
        },
    )
    files_deleted = _unlink_files(files)
    for row in rows:
        session.delete(row)
    session.commit()
    logger.info(
        "retention purge: deleted=%d files=%d cutoff=%s",
        len(rows),
        files_deleted,
        cutoff.date().isoformat(),
    )
    return RetentionResult(
        dry_run=False,
        retention_years=years,
        cutoff=cutoff,
        candidates=candidates,
        deleted=len(rows),
        files_deleted=files_deleted,
        audit_id=entry.id,
    )

