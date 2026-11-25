"""Retention tests: read-only dry runs, token-gated purge and auditability.

Acceptance criteria covered here:

* a dry run lists expired documents and deletes neither rows nor files (and writes no audit
  entry);
* ``dry_run=false`` without a token, with a wrong token, or with the token unconfigured is
  rejected with ``403`` and changes nothing;
* a confirmed run deletes exactly the expired documents and their files, leaves newer rows
  untouched and records one PII-free ``RETENTION_PURGE`` audit entry;
* a file still referenced by a retained document is never removed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Document
from app.domain.enums import DocumentSource, DocumentStatus, DocumentType
from app.services import retention
from app.settings import get_settings

TOKEN = "test-retention-confirmation-token"
TOKEN_HEADER = "X-Retention-Token"
RETENTION_ENDPOINT = "/api/admin/retention/run"


@pytest.fixture
def documents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated storage plus the token/retention window the endpoint reads from ENV."""
    directory = tmp_path / "documents"
    monkeypatch.setenv("DOCUMENTS_DIR", str(directory))
    monkeypatch.setenv("RETENTION_CONFIRMATION_TOKEN", TOKEN)
    monkeypatch.setenv("RETENTION_YEARS", "5")
    get_settings.cache_clear()
    yield directory
    get_settings.cache_clear()


def _document(
    session: Session,
    directory: Path,
    *,
    created_at: datetime,
    content: bytes | None = None,
    file_name: str | None = None,
) -> Document:
    """Create one stored document with an explicit age (``created_at``)."""
    payload = content or f"document-{created_at.isoformat()}-{file_name or ''}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    stored_name = file_name or f"{digest}.pdf"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / stored_name
    if not path.exists():
        path.write_bytes(payload)
    row = Document(
        type=DocumentType.VAT_INVOICE_COST,
        issue_date=created_at.date(),
        period=f"{created_at:%Y-%m}",
        amount_gross=Decimal("10.00"),
        source=DocumentSource.UPLOAD,
        file_path=stored_name,
        sha256=digest,
        status=DocumentStatus.CLASSIFIED,
        created_at=created_at,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _run(
    client: TestClient,
    *,
    dry_run: bool = True,
    token: str | None = None,
):
    headers = {TOKEN_HEADER: token} if token is not None else {}
    return client.post(
        RETENTION_ENDPOINT, params={"dry_run": str(dry_run).lower()}, headers=headers
    )


# --------------------------------------------------------------------------- dry run


def test_dry_run_lists_expired_documents_and_deletes_nothing(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    old = _document(session, documents_dir, created_at=datetime.now(UTC) - timedelta(days=365 * 6))
    fresh = _document(session, documents_dir, created_at=datetime.now(UTC) - timedelta(days=30))

    response = _run(api_client)  # dry_run defaults to true

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["retention_years"] == 5
    assert body["deleted"] == 0
    assert body["files_deleted"] == 0
    assert body["audit_id"] is None
    assert [candidate["id"] for candidate in body["candidates"]] == [old.id]
    assert body["candidates"][0]["document_type"] == DocumentType.VAT_INVOICE_COST.value

    # Nothing was modified: both rows and both files are still there, no audit entry exists.
    assert session.get(Document, old.id) is not None
    assert session.get(Document, fresh.id) is not None
    assert (documents_dir / (old.file_path or "")).is_file()
    assert (documents_dir / (fresh.file_path or "")).is_file()
    assert session.scalars(select(AuditLog)).all() == []


def test_plan_keeps_a_document_exactly_at_the_cutoff(session: Session, documents_dir: Path) -> None:
    """``created_at == cutoff`` is still retained; only strictly older rows expire."""
    fixed_now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    cutoff = retention.cutoff_for(5, now=fixed_now)
    just_expired = _document(session, documents_dir, created_at=cutoff - timedelta(seconds=1))
    at_cutoff = _document(session, documents_dir, created_at=cutoff)

    plan = retention.build_plan(session, now=fixed_now)

    assert [candidate.id for candidate in plan.candidates] == [just_expired.id]
    assert at_cutoff.id not in {candidate.id for candidate in plan.candidates}


# --------------------------------------------------------------------- confirmation


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_destructive_run_without_a_valid_token_is_forbidden_and_changes_nothing(
    api_client: TestClient,
    documents_dir: Path,
    session: Session,
    token: str | None,
) -> None:
    old = _document(session, documents_dir, created_at=datetime.now(UTC) - timedelta(days=365 * 6))

    response = _run(api_client, dry_run=False, token=token)

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["header"] == TOKEN_HEADER
    assert session.get(Document, old.id) is not None
    assert (documents_dir / (old.file_path or "")).is_file()
    assert session.scalars(select(AuditLog)).all() == []


def test_destructive_run_is_forbidden_when_no_token_is_configured(
    api_client: TestClient,
    documents_dir: Path,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RETENTION_CONFIRMATION_TOKEN", raising=False)
    get_settings.cache_clear()
    old = _document(session, documents_dir, created_at=datetime.now(UTC) - timedelta(days=365 * 6))

    response = _run(api_client, dry_run=False, token="anything")

    assert response.status_code == 403, response.text
    assert session.get(Document, old.id) is not None
    assert session.scalars(select(AuditLog)).all() == []


# ---------------------------------------------------------------------------- purge


def test_confirmed_run_deletes_only_expired_documents_and_writes_an_audit_entry(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    now = datetime.now(UTC)
    old_6y = _document(session, documents_dir, created_at=now - timedelta(days=365 * 6))
    old_7y = _document(session, documents_dir, created_at=now - timedelta(days=365 * 7))
    fresh = _document(session, documents_dir, created_at=now - timedelta(days=365))

    response = _run(api_client, dry_run=False, token=TOKEN)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    assert body["deleted"] == 2
    assert body["files_deleted"] == 2
    assert sorted(candidate["id"] for candidate in body["candidates"]) == sorted(
        [old_6y.id, old_7y.id]
    )
    assert body["audit_id"] is not None

    # Only expired rows and files are gone; the newer one is untouched.
    assert session.get(Document, old_6y.id) is None
    assert session.get(Document, old_7y.id) is None
    assert session.get(Document, fresh.id) is not None
    assert not (documents_dir / (old_6y.file_path or "")).exists()
    assert not (documents_dir / (old_7y.file_path or "")).exists()
    assert (documents_dir / (fresh.file_path or "")).is_file()

    entries = list(session.scalars(select(AuditLog)).all())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == body["audit_id"]
    assert entry.action == "RETENTION_PURGE"
    assert entry.actor == "admin"
    assert entry.entity == "document"
    assert entry.meta["deleted"] == 2
    assert entry.meta["files"] == 2
    assert entry.meta["retention_years"] == 5
    assert entry.meta["dry_run"] is False
    # The audit entry holds counts only - no document ids or file names.
    assert set(entry.meta) == {
        "retention_years",
        "cutoff",
        "candidates",
        "deleted",
        "files",
        "dry_run",
    }
    serialized = json.dumps(entry.meta)
    assert (old_6y.file_path or "no-file") not in serialized


def test_purge_aborts_before_touching_data_when_the_audit_write_fails(
    documents_dir: Path,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _document(session, documents_dir, created_at=datetime.now(UTC) - timedelta(days=365 * 6))

    def _broken_record(*args: object, **kwargs: object) -> object:
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(retention.audit, "record", _broken_record)
    with pytest.raises(RuntimeError):
        retention.run_retention(session, dry_run=False, confirmation_token=TOKEN)
    session.rollback()

    # The audit row is flushed before the first unlink, so a failure leaves everything intact.
    assert session.get(Document, old.id) is not None
    assert (documents_dir / (old.file_path or "")).is_file()


def test_a_file_still_used_by_a_retained_document_is_not_deleted(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    shared_content = b"shared content-addressed payload"
    shared_name = f"{hashlib.sha256(shared_content).hexdigest()}.pdf"
    now = datetime.now(UTC)
    expired = _document(
        session,
        documents_dir,
        created_at=now - timedelta(days=365 * 6),
        content=shared_content,
        file_name=shared_name,
    )
    retained = _document(
        session,
        documents_dir,
        created_at=now - timedelta(days=30),
        content=shared_content,
        file_name=shared_name,
    )

    response = _run(api_client, dry_run=False, token=TOKEN)

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert response.json()["files_deleted"] == 0
    assert session.get(Document, expired.id) is None
    assert session.get(Document, retained.id) is not None
    assert (documents_dir / shared_name).is_file()
