"""Audit-trail and data-export tests.

Acceptance criteria covered here:

* upload, status change and export append ``AuditLog`` rows without personal data;
* the application log does not contain the client file name, document contents or an
  e-mail/PESEL-shaped marker;
* ``GET /api/admin/export-data`` returns every stored personal-data category;
* the audit metadata validator rejects unknown keys, nested values and PII-shaped strings;
* revision ``0003`` (``audit_log``) upgrades, downgrades and re-applies cleanly.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.db.models import (
    AssistantAnswer,
    AuditLog,
    Deadline,
    Decision,
    Document,
    TaxProfile,
)
from app.domain.enums import (
    DeadlineKind,
    DeadlineStatus,
    DocumentSource,
    DocumentStatus,
    DocumentType,
    PitRegime,
    VatStatus,
    ZusVariant,
)
from app.services import audit
from app.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PDF_CONTENT_TYPE = "application/pdf"


@pytest.fixture
def documents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point upload storage at a per-test directory."""
    directory = tmp_path / "documents"
    monkeypatch.setenv("DOCUMENTS_DIR", str(directory))
    get_settings.cache_clear()
    return directory


def _pdf(body: bytes = b"") -> bytes:
    return b"%PDF-1.7\n" + body + b"\n%%EOF\n"


def _upload(client: TestClient, *, filename: str, content: bytes):
    return client.post(
        "/api/documents", files={"file": (filename, content, PDF_CONTENT_TYPE)}
    )


def _audit_rows(session: Session) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.id)).all())


# ------------------------------------------------------------------------- auditing


def test_upload_is_audited_and_neither_audit_nor_logs_contain_pii(
    api_client: TestClient,
    documents_dir: Path,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "PESEL-44051401359-SECRET-MARKER"
    email = "jan.kowalski@example.com"
    filename = "Jan_Kowalski_44051401359.pdf"
    content = _pdf(f"Faktura VAT\n{marker}\n{email}\n".encode())

    with caplog.at_level(logging.DEBUG):
        response = _upload(api_client, filename=filename, content=content)

    assert response.status_code == 201, response.text
    body = response.json()

    entries = _audit_rows(session)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == audit.AuditAction.DOCUMENT_UPLOADED.value
    assert entry.actor == audit.DEFAULT_ACTOR
    assert entry.entity == "document"
    assert entry.entity_id == str(body["id"])
    assert entry.meta["type"] == body["type"]
    assert entry.meta["status"] == body["status"]
    assert entry.meta["source"] == DocumentSource.UPLOAD.value
    assert entry.meta["bytes"] == len(content)

    serialized = json.dumps(entry.meta)
    for needle in (marker, email, "Jan_Kowalski", filename, body["file_name"], str(documents_dir)):
        assert needle not in serialized

    messages = "\n".join(record.getMessage() for record in caplog.records)
    for needle in (marker, email, "Jan_Kowalski", filename, str(documents_dir)):
        assert needle not in messages
    assert "document ingested" in messages
    assert "audit: action=DOCUMENT_UPLOADED" in messages


def test_status_change_is_audited(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    created = _upload(
        api_client, filename="scan-0001.pdf", content=_pdf(b"nieczytelny skan")
    ).json()

    status_response = api_client.patch(
        f"/api/documents/{created['id']}", json={"status": DocumentStatus.VERIFIED.value}
    )
    type_response = api_client.patch(
        f"/api/documents/{created['id']}", json={"type": DocumentType.ZUS.value}
    )

    assert status_response.status_code == 200, status_response.text
    assert type_response.status_code == 200, type_response.text

    entries = _audit_rows(session)
    assert [entry.action for entry in entries] == [
        audit.AuditAction.DOCUMENT_UPLOADED.value,
        audit.AuditAction.DOCUMENT_STATUS_CHANGED.value,
        audit.AuditAction.DOCUMENT_UPDATED.value,
    ]
    status_entry = entries[1]
    assert status_entry.meta["previous_status"] == created["status"]
    assert status_entry.meta["status"] == DocumentStatus.VERIFIED.value
    assert status_entry.entity_id == str(created["id"])
    type_entry = entries[2]
    assert type_entry.meta["type"] == DocumentType.ZUS.value
    assert type_entry.meta["status"] == DocumentStatus.VERIFIED.value


# ---------------------------------------------------------------------- data export


def test_export_data_returns_every_stored_category(
    api_client: TestClient, session: Session
) -> None:
    profile = TaxProfile(
        pit_regime=PitRegime.SCALE,
        vat_status=VatStatus.REGISTERED,
        vat_exemption_threshold_pln=Decimal("200000.00"),
        zus_variant=ZusVariant.FULL,
        valid_from=date(2026, 1, 1),
    )
    document = Document(
        type=DocumentType.VAT_INVOICE_COST,
        issue_date=date(2026, 3, 14),
        period="2026-03",
        amount_net=Decimal("1000.00"),
        vat_amount=Decimal("230.00"),
        amount_gross=Decimal("1230.00"),
        counterparty_nip="5260250995",
        source=DocumentSource.UPLOAD,
        file_path="a" * 64 + ".pdf",
        sha256="a" * 64,
        status=DocumentStatus.VERIFIED,
    )
    deadline = Deadline(
        kind=DeadlineKind.VAT_DECLARATION,
        period="2026-03",
        due_date=date(2026, 4, 25),
        status=DeadlineStatus.OPEN,
        notes="sprawdzic JPK",
    )
    decision = Decision(
        decided_at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        topic="vat_registration",
        choice="stay_registered",
        rationale="Planowany obrot przekracza limit.",
        source_refs=["https://example.gov.pl/interpretation"],
    )
    answer = AssistantAnswer(
        question="Czy moge odliczyc laptopa?",
        answer="Tak, jesli sluzy dzialalnosci - sprawdz cytowane zrodlo.",
        citations=[{"url": "https://example.gov.pl/a", "title": "Interpretacja"}],
        model="test-model",
    )
    session.add_all([profile, document, deadline, decision, answer])
    session.commit()
    audit.record(
        session,
        action=audit.AuditAction.DOCUMENT_UPLOADED,
        entity="document",
        entity_id=document.id,
        meta={"type": document.type.value, "status": document.status.value},
    )
    session.commit()

    response = api_client.get("/api/admin/export-data")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "taxwise-data-export.json" in response.headers["content-disposition"]
    body = response.json()
    assert body["export_version"] == 1
    assert body["retention_years"] == 5
    assert body["counts"] == {
        "profiles": 1,
        "documents": 1,
        "deadlines": 1,
        "decisions": 1,
        "answers": 1,
        "rows": 1,
    }

    assert body["tax_profiles"][0]["pit_regime"] == PitRegime.SCALE.value
    assert body["tax_profiles"][0]["vat_exemption_threshold_pln"] == "200000.00"
    assert body["documents"][0]["amount_gross"] == "1230.00"
    assert body["documents"][0]["counterparty_nip"] == "5260250995"
    assert body["documents"][0]["status"] == DocumentStatus.VERIFIED.value
    assert body["deadlines"][0]["notes"] == "sprawdzic JPK"
    assert body["decisions"][0]["rationale"] == "Planowany obrot przekracza limit."
    assert body["decisions"][0]["source_refs"] == ["https://example.gov.pl/interpretation"]
    assert body["assistant_answers"][0]["question"] == "Czy moge odliczyc laptopa?"
    assert body["assistant_answers"][0]["citations"][0]["title"] == "Interpretacja"
    assert body["audit_log"][0]["action"] == audit.AuditAction.DOCUMENT_UPLOADED.value

    # The export itself is audited; its meta carries counts only.
    entries = _audit_rows(session)
    assert [entry.action for entry in entries] == [
        audit.AuditAction.DOCUMENT_UPLOADED.value,
        audit.AuditAction.EXPORT_DATA.value,
    ]
    export_entry = entries[-1]
    assert export_entry.actor == "admin"
    assert export_entry.entity == "user-data"
    assert export_entry.meta["documents"] == 1
    assert export_entry.meta["profiles"] == 1
    assert "5260250995" not in json.dumps(export_entry.meta)


# --------------------------------------------------------------- metadata validation


def test_sanitize_meta_accepts_only_allowlisted_scalars() -> None:
    cleaned = audit.sanitize_meta({"year": 2026, "kind": "jpk-pit", "dry_run": True})
    assert cleaned == {"year": 2026, "kind": "jpk-pit", "dry_run": True}


@pytest.mark.parametrize(
    "meta",
    [
        {"note": "free text"},  # unknown key
        {"kind": "Jan Kowalski"},  # free-form text with a space
        {"kind": ["a", "b"]},  # nested value
        {"kind": {"name": "Jan"}},  # nested value
        {"kind": "jan.kowalski@example.com"},  # e-mail shape
        {"kind": "44051401359"},  # PESEL shape
        {"kind": "5260250995"},  # NIP shape
    ],
)
def test_audit_record_rejects_unsafe_metadata(session: Session, meta: dict[str, object]) -> None:
    with pytest.raises(audit.AuditMetadataError):
        audit.record(
            session,
            action=audit.AuditAction.DOCUMENT_UPLOADED,
            entity="document",
            entity_id=1,
            meta=meta,
        )
    assert _audit_rows(session) == []


# ------------------------------------------------------------------------ migration


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def test_audit_log_migration_upgrade_downgrade_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision 0003 creates ``audit_log``, ``downgrade -1`` removes only that table."""
    url = f"sqlite+pysqlite:///{tmp_path / 'audit_migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = _alembic_config()

    command.upgrade(config, "head")
    command.check(config)  # migration == ORM metadata
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "audit_log" in tables

    command.downgrade(config, "-1")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "audit_log" not in tables
    assert "documents" in tables  # only the audit revision was rolled back

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "audit_log" in tables


