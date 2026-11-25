"""Administrative endpoints: retention runs and the GDPR data export.

``POST /api/admin/retention/run?dry_run=true|false``
    ``dry_run=true`` (the default) is a read-only scan: it lists the documents older than
    ``RETENTION_YEARS`` and changes nothing. ``dry_run=false`` irreversibly deletes the
    files and rows *only* when the ``X-Retention-Token`` header matches
    ``RETENTION_CONFIRMATION_TOKEN`` from the environment; a missing or invalid token is
    answered with ``403`` before any data is touched. Every completed purge appends one
    ``RETENTION_PURGE`` row to ``audit_log``.

``GET /api/admin/export-data``
    Returns one JSON document with every personal-data category the application stores
    (documents and their file metadata, tax profiles, deadlines, decisions, assistant
    answers and the PII-free audit trail). The response is marked ``no-store`` and the
    export itself is audited as ``EXPORT_DATA``.

The response of the export is personal data: it is generated in memory and never cached
or logged. No endpoint in this module writes personal data to the application log.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AssistantAnswer,
    AuditLog,
    Deadline,
    Decision,
    Document,
    TaxProfile,
)
from app.db.session import get_session
from app.services import audit
from app.services import retention as retention_service
from app.settings import Settings, get_settings

__all__ = ["RETENTION_TOKEN_HEADER", "RetentionCandidateRead", "RetentionRunResponse", "router"]

router = APIRouter(prefix="/api/admin", tags=["admin"])

logger = logging.getLogger(__name__)

#: Header carrying the confirmation token for an irreversible purge. A header (not a query
#: parameter) keeps the secret out of URLs, browser history and access logs.
RETENTION_TOKEN_HEADER: Final[str] = "X-Retention-Token"

#: Actor recorded for operator-triggered administrative actions.
_ADMIN_ACTOR: Final[str] = "admin"


class RetentionCandidateRead(BaseModel):
    """One expired document in a retention scan."""

    id: int
    document_type: str
    source: str
    period: str
    created_at: datetime
    file_name: str | None = Field(
        default=None, description="Content-addressed file name, relative to DOCUMENTS_DIR."
    )


class RetentionRunResponse(BaseModel):
    """Result of a dry run or of a completed purge."""

    dry_run: bool = Field(description="True for a read-only scan; false for a purge.")
    retention_years: int = Field(description="Window applied to documents.created_at.")
    cutoff: datetime = Field(description="Documents created before this instant expired.")
    candidates: list[RetentionCandidateRead]
    deleted: int = Field(description="Deleted document rows (0 for a dry run).")
    files_deleted: int = Field(description="Deleted files (0 for a dry run).")
    audit_id: int | None = Field(
        default=None, description="audit_log row of a purge; null for a dry run."
    )


def _candidate_read(
    candidate: retention_service.RetentionCandidate,
) -> RetentionCandidateRead:
    return RetentionCandidateRead(
        id=candidate.id,
        document_type=candidate.document_type,
        source=candidate.source,
        period=candidate.period,
        created_at=candidate.created_at,
        file_name=candidate.file_name,
    )


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _money(value: Decimal | None) -> str | None:
    """Money as an exact decimal string - never a float (avoids rounding in the export)."""
    return None if value is None else f"{value:.2f}"


@router.post(
    "/retention/run",
    response_model=RetentionRunResponse,
    summary="Scan (dry run) or irreversibly purge expired documents",
    responses={
        http_status.HTTP_403_FORBIDDEN: {
            "description": "Missing or invalid X-Retention-Token for dry_run=false"
        },
        http_status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "The purge could not delete a file or complete the transaction"
        },
    },
)
def run_retention(
    session: Annotated[Session, Depends(get_session)],
    dry_run: Annotated[
        bool,
        Query(description="true = list candidates only (default); false = irreversible purge."),
    ] = True,
    retention_token: Annotated[
        str | None,
        Header(
            alias=RETENTION_TOKEN_HEADER,
            description="Required when dry_run=false; must match RETENTION_CONFIRMATION_TOKEN.",
        ),
    ] = None,
) -> RetentionRunResponse:
    """Dry-run by default; delete files and rows only with a valid confirmation token."""
    try:
        result = retention_service.run_retention(
            session,
            dry_run=dry_run,
            confirmation_token=retention_token,
            actor=_ADMIN_ACTOR,
        )
    except retention_service.RetentionConfirmationError as exc:
        session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail={
                "message": "A valid confirmation token is required for a destructive run.",
                "header": RETENTION_TOKEN_HEADER,
                "dry_run": dry_run,
            },
        ) from exc
    except retention_service.RetentionError as exc:
        session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The retention purge failed; the database transaction was rolled back. "
                "Inspect DOCUMENTS_DIR for partially deleted files."
            ),
        ) from exc

    return RetentionRunResponse(
        dry_run=result.dry_run,
        retention_years=result.retention_years,
        cutoff=result.cutoff,
        candidates=[_candidate_read(candidate) for candidate in result.candidates],
        deleted=result.deleted,
        files_deleted=result.files_deleted,
        audit_id=result.audit_id,
    )


def _document_json(row: Document) -> dict[str, object]:
    return {
        "id": row.id,
        "type": row.type.value,
        "status": row.status.value,
        "source": row.source.value,
        "period": row.period,
        "issue_date": _iso(row.issue_date),
        "amount_net": _money(row.amount_net),
        "vat_amount": _money(row.vat_amount),
        "amount_gross": _money(row.amount_gross),
        "currency": row.currency,
        "counterparty_nip": row.counterparty_nip,
        "external_id": row.external_id,
        "file_name": row.file_path,
        "sha256": row.sha256,
        "created_at": _iso(row.created_at),
    }


def _profile_json(row: TaxProfile) -> dict[str, object]:
    return {
        "id": row.id,
        "pit_regime": row.pit_regime.value,
        "vat_status": row.vat_status.value,
        "vat_exemption_threshold_pln": _money(row.vat_exemption_threshold_pln),
        "zus_variant": row.zus_variant.value,
        "ip_box_rate": None if row.ip_box_rate is None else str(row.ip_box_rate),
        "valid_from": _iso(row.valid_from),
        "valid_to": _iso(row.valid_to),
    }


def _deadline_json(row: Deadline) -> dict[str, object]:
    return {
        "id": row.id,
        "kind": row.kind.value,
        "period": row.period,
        "due_date": _iso(row.due_date),
        "status": row.status.value,
        "notes": row.notes,
    }


def _decision_json(row: Decision) -> dict[str, object]:
    return {
        "id": row.id,
        "decided_at": _iso(row.decided_at),
        "topic": row.topic,
        "choice": row.choice,
        "rationale": row.rationale,
        "source_refs": row.source_refs,
    }


def _answer_json(row: AssistantAnswer) -> dict[str, object]:
    return {
        "id": row.id,
        "question": row.question,
        "answer": row.answer,
        "citations": row.citations,
        "model": row.model,
        "created_at": _iso(row.created_at),
        "disclaimer_shown": row.disclaimer_shown,
    }


def _audit_json(row: AuditLog) -> dict[str, object]:
    return {
        "id": row.id,
        "actor": row.actor,
        "action": row.action,
        "entity": row.entity,
        "entity_id": row.entity_id,
        "at": _iso(row.at),
        "meta": row.meta,
    }


def _export_payload(
    session: Session, settings: Settings
) -> tuple[dict[str, object], dict[str, object]]:
    """Collect every stored personal-data category into one JSON-safe payload."""
    profiles = list(session.scalars(select(TaxProfile).order_by(TaxProfile.id)).all())
    documents = list(session.scalars(select(Document).order_by(Document.id)).all())
    deadlines = list(session.scalars(select(Deadline).order_by(Deadline.id)).all())
    decisions = list(session.scalars(select(Decision).order_by(Decision.id)).all())
    answers = list(session.scalars(select(AssistantAnswer).order_by(AssistantAnswer.id)).all())
    audit_rows = list(session.scalars(select(AuditLog).order_by(AuditLog.id)).all())

    counts: dict[str, object] = {
        "profiles": len(profiles),
        "documents": len(documents),
        "deadlines": len(deadlines),
        "decisions": len(decisions),
        "answers": len(answers),
        "rows": len(audit_rows),
    }
    payload: dict[str, object] = {
        "export_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "retention_years": settings.retention_years,
        "counts": counts,
        "tax_profiles": [_profile_json(row) for row in profiles],
        "documents": [_document_json(row) for row in documents],
        "deadlines": [_deadline_json(row) for row in deadlines],
        "decisions": [_decision_json(row) for row in decisions],
        "assistant_answers": [_answer_json(row) for row in answers],
        # The audit trail is included for transparency; it deliberately holds no PII.
        "audit_log": [_audit_json(row) for row in audit_rows],
    }
    return payload, counts


@router.get(
    "/export-data",
    response_class=JSONResponse,
    summary="Export every stored category of personal data as JSON",
    responses={
        http_status.HTTP_200_OK: {
            "description": (
                "Complete user-data export; contains personal data, so it is marked "
                "Cache-Control: no-store and should be handled as a confidential file."
            )
        }
    },
)
def export_data(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Return all user data and record one PII-free ``EXPORT_DATA`` audit entry."""
    payload, counts = _export_payload(session, settings)
    audit.record(
        session,
        actor=_ADMIN_ACTOR,
        action=audit.AuditAction.EXPORT_DATA,
        entity="user-data",
        entity_id="all",
        meta=counts,
    )
    session.commit()
    logger.info(
        "data export: documents=%s deadlines=%s decisions=%s answers=%s",
        counts["documents"],
        counts["deadlines"],
        counts["decisions"],
        counts["answers"],
    )
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": 'attachment; filename="taxwise-data-export.json"',
            "Cache-Control": "no-store",
        },
    )

