"""Document ingestion tests: secure upload, dedupe, classification and correction.

Acceptance criteria covered here: a PDF upload returns ``201`` with a heuristic type and
is stored under its SHA-256 name; re-uploading the same bytes returns ``409`` with the
existing id and creates neither a second row nor a second file; files above 10 MB are
rejected with ``413``; an unrecognised document becomes ``OTHER``; ``PATCH`` corrects the
type/status; traversal-shaped client names are rejected; and neither contents nor storage
paths reach the logs.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.domain.enums import DocumentSource, DocumentStatus, DocumentType
from app.services import classifier, storage
from app.services.deadlines import today_in_warsaw
from app.settings import get_settings

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
PDF_CONTENT_TYPE = "application/pdf"


@pytest.fixture
def documents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point storage at a per-test directory; tests never write into the repository."""
    directory = tmp_path / "documents"
    monkeypatch.setenv("DOCUMENTS_DIR", str(directory))
    get_settings.cache_clear()
    yield directory
    get_settings.cache_clear()


def _pdf(body: bytes = b"") -> bytes:
    """Minimal PDF-shaped payload; intake validates magic bytes, not the PDF layout."""
    return b"%PDF-1.7\n" + body + b"\n%%EOF\n"


def _upload(
    client: TestClient,
    *,
    filename: str,
    content: bytes,
    content_type: str = PDF_CONTENT_TYPE,
):
    return client.post("/api/documents", files={"file": (filename, content, content_type)})


def _documents(session: Session) -> list[Document]:
    return list(session.scalars(select(Document)))


def _stored_files(directory: Path) -> list[Path]:
    return sorted(directory.iterdir()) if directory.exists() else []


# --------------------------------------------------------------------------- upload


def test_upload_pdf_is_classified_and_stored_under_sha256(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    content = _pdf(b"Faktura VAT nr 12/2026\nSprzedawca: ACME sp. z o.o.\n")

    response = _upload(api_client, filename="faktura_2026_03.pdf", content=content)

    assert response.status_code == 201, response.text
    body = response.json()
    digest = hashlib.sha256(content).hexdigest()
    today = today_in_warsaw()

    assert body["type"] == DocumentType.VAT_INVOICE_COST.value
    assert body["status"] == DocumentStatus.CLASSIFIED.value
    assert body["source"] == DocumentSource.UPLOAD.value
    assert body["sha256"] == digest
    assert body["file_name"] == f"{digest}.pdf"
    assert body["file_name"] != "faktura_2026_03.pdf"
    assert body["period"] == f"{today:%Y-%m}"
    assert body["issue_date"] == today.isoformat()
    assert Decimal(str(body["amount_gross"])) == Decimal("0.00")

    stored = documents_dir / f"{digest}.pdf"
    assert stored.is_file()
    assert stored.read_bytes() == content
    # The atomic write leaves no temporary files behind.
    assert list(documents_dir.glob(f"{storage.TEMP_PREFIX}*")) == []

    row = session.get(Document, body["id"])
    assert row is not None
    assert row.sha256 == digest
    assert row.file_path == f"{digest}.pdf"


def test_duplicate_upload_returns_409_with_the_existing_id(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    content = _pdf(b"Faktura VAT nr 1/2026\n")
    first = _upload(api_client, filename="faktura_a.pdf", content=content)
    assert first.status_code == 201, first.text

    duplicate = _upload(api_client, filename="kopia_faktury.pdf", content=content)

    assert duplicate.status_code == 409, duplicate.text
    detail = duplicate.json()["detail"]
    assert detail["existing_id"] == first.json()["id"]
    assert len(_documents(session)) == 1
    assert len(_stored_files(documents_dir)) == 1


def test_oversized_upload_is_rejected_with_413(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    content = _pdf(b"A" * MAX_DOCUMENT_BYTES)

    response = _upload(api_client, filename="huge.pdf", content=content)

    assert response.status_code == 413, response.text
    assert _documents(session) == []
    assert _stored_files(documents_dir) == []


def test_unknown_document_is_classified_as_other(
    api_client: TestClient, documents_dir: Path
) -> None:
    response = _upload(
        api_client,
        filename="scan-0001.pdf",
        content=_pdf(b"Lorem ipsum dolor sit amet, consectetur adipiscing elit."),
    )

    assert response.status_code == 201, response.text
    assert response.json()["type"] == DocumentType.OTHER.value


def test_empty_upload_is_rejected(api_client: TestClient, session: Session) -> None:
    response = _upload(api_client, filename="empty.pdf", content=b"")

    assert response.status_code == 400
    assert _documents(session) == []


def test_xml_and_csv_uploads_are_accepted(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<JPK xmlns="http://crd.gov.pl/wzor/2026/01/01/1234/"><Naglowek/></JPK>'
    )
    csv = "NIP;Kwota\n5260250995;100,00\n".encode("cp1250")

    xml_response = _upload(
        api_client, filename="jpk_v7m_2026_03.xml", content=xml, content_type="application/xml"
    )
    csv_response = _upload(
        api_client, filename="zus_dra_2026_03.csv", content=csv, content_type="text/csv"
    )

    assert xml_response.status_code == 201, xml_response.text
    assert xml_response.json()["type"] == DocumentType.VAT_INVOICE_SALES.value
    assert csv_response.status_code == 201, csv_response.text
    assert csv_response.json()["type"] == DocumentType.ZUS.value
    assert len(_documents(session)) == 2


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("faktura.pdf", b"kolumna;wartosc\n1;2\n", PDF_CONTENT_TYPE),
        ("faktura.pdf", _pdf(), "text/csv"),
        ("faktura.exe", _pdf(), "application/octet-stream"),
        ("faktura.pdf", b"\x00\x01\x02\x03", PDF_CONTENT_TYPE),
    ],
)
def test_upload_rejects_unsupported_name_mime_or_content(
    api_client: TestClient,
    session: Session,
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    response = _upload(api_client, filename=filename, content=content, content_type=content_type)

    assert response.status_code == 415, response.text
    assert _documents(session) == []


@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd.pdf",
        "..\\..\\windows\\system32\\evil.pdf",
        "/etc/passwd.pdf",
        "..",
        ".hidden.pdf",
    ],
)
def test_path_traversal_in_file_name_is_rejected(
    api_client: TestClient, documents_dir: Path, session: Session, filename: str
) -> None:
    response = _upload(api_client, filename=filename, content=_pdf(b"Faktura VAT"))

    assert response.status_code == 400, response.text
    assert _documents(session) == []
    assert _stored_files(documents_dir) == []


# --------------------------------------------------------------------------- patch


def test_patch_corrects_type_and_status(
    api_client: TestClient, documents_dir: Path, session: Session
) -> None:
    created = _upload(
        api_client, filename="scan-0002.pdf", content=_pdf(b"nieczytelny skan")
    ).json()
    assert created["type"] == DocumentType.OTHER.value

    corrected = api_client.patch(
        f"/api/documents/{created['id']}", json={"type": DocumentType.ZUS.value}
    )

    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["type"] == DocumentType.ZUS.value
    assert corrected.json()["status"] == created["status"]

    verified = api_client.patch(
        f"/api/documents/{created['id']}", json={"status": DocumentStatus.VERIFIED.value}
    )

    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == DocumentStatus.VERIFIED.value
    assert verified.json()["type"] == DocumentType.ZUS.value

    row = session.get(Document, created["id"])
    assert row is not None
    assert row.type is DocumentType.ZUS
    assert row.status is DocumentStatus.VERIFIED


def test_patch_requires_a_field_and_an_existing_document(
    api_client: TestClient, documents_dir: Path
) -> None:
    created = _upload(api_client, filename="faktura_patch.pdf", content=_pdf(b"Faktura")).json()

    empty = api_client.patch(f"/api/documents/{created['id']}", json={})
    unknown_field = api_client.patch(
        f"/api/documents/{created['id']}", json={"amount_gross": "10.00"}
    )
    missing = api_client.patch("/api/documents/9999", json={"type": DocumentType.OTHER.value})

    assert empty.status_code == 422
    assert unknown_field.status_code == 422
    assert missing.status_code == 404


# --------------------------------------------------------------------------- logging


def test_upload_does_not_log_contents_names_or_paths(
    api_client: TestClient, documents_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "PESEL-44051401359-SECRET-MARKER"
    filename = "Jan_Kowalski_44051401359.pdf"
    content = _pdf(f"Faktura VAT\n{marker}\n".encode())

    with caplog.at_level(logging.DEBUG):
        response = _upload(api_client, filename=filename, content=content)

    assert response.status_code == 201, response.text
    messages = "\n".join(record.getMessage() for record in caplog.records)
    for needle in (marker, "Jan_Kowalski", str(documents_dir), response.json()["file_name"]):
        assert needle not in messages
    # The only document-related log line carries the id and size.
    assert "document ingested" in messages


# ----------------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("faktura_2026_03.pdf", DocumentType.VAT_INVOICE_COST),
        ("FA(2026)03.pdf", DocumentType.VAT_INVOICE_COST),
        ("faktura_zakupu_2026_03.pdf", DocumentType.VAT_INVOICE_COST),
        ("faktura_sprzedazy_2026_03.pdf", DocumentType.VAT_INVOICE_SALES),
        ("jpk_v7m_2026_03.xml", DocumentType.VAT_INVOICE_SALES),
        ("zus_dra_2026_03.pdf", DocumentType.ZUS),
        ("PIT-36_2025.pdf", DocumentType.PIT),
        ("deklaracja_roczna_2025.pdf", DocumentType.PIT),
        ("scan-0001.pdf", DocumentType.OTHER),
    ],
)
def test_classifier_matches_file_names(filename: str, expected: DocumentType) -> None:
    assert classifier.classify(filename) is expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"%PDF-1.7\nFaktura VAT nr 1/2026\n", DocumentType.VAT_INVOICE_COST),
        ("ZUS DRA za 03/2026".encode("cp1250"), DocumentType.ZUS),
        ("Faktura sprzedaży nr 7/2026".encode("cp1250"), DocumentType.VAT_INVOICE_SALES),
        (b"<?xml version='1.0'?><JPK></JPK>", DocumentType.VAT_INVOICE_SALES),
        (b"%PDF-1.7\nnotarialny skan umowy\n", DocumentType.OTHER),
    ],
)
def test_classifier_falls_back_to_content(content: bytes, expected: DocumentType) -> None:
    assert classifier.classify("scan-0001.pdf", content) is expected


def test_file_name_wins_over_content() -> None:
    # The name is the stronger signal; contents may mention several taxes at once.
    assert classifier.classify("zus_dra_03.pdf", _pdf(b"Faktura VAT")) is DocumentType.ZUS


def test_classifier_ignores_binary_noise() -> None:
    assert classifier.classify("scan.pdf", b"\x00\xff\xfe" * 100) is DocumentType.OTHER


# -------------------------------------------------------------------------- storage


def test_storage_is_content_addressed_and_atomic(tmp_path: Path) -> None:
    content = _pdf(b"Faktura VAT")
    digest = hashlib.sha256(content).hexdigest()

    first = storage.save_document(content, sha256=digest, extension=".pdf", root=tmp_path)
    second = storage.save_document(content, sha256=digest, extension=".pdf", root=tmp_path)

    assert first.created is True
    assert second.created is False
    assert first.path == tmp_path.resolve() / f"{digest}.pdf"
    assert first.path.read_bytes() == content
    assert list(tmp_path.glob(f"{storage.TEMP_PREFIX}*")) == []


@pytest.mark.parametrize(
    "filename",
    ["../../etc/passwd.pdf", "..\\evil.pdf", "/etc/passwd.pdf", "..", ".env", "\x00.pdf"],
)
def test_sanitize_filename_rejects_traversal(filename: str) -> None:
    with pytest.raises(storage.UnsafeFilenameError):
        storage.sanitize_filename(filename)


def test_extension_allowlist_is_case_insensitive() -> None:
    assert storage.extension_for("FAKTURA.PDF") == ".pdf"
    assert storage.extension_for(" jpk.xml ") == ".xml"
    with pytest.raises(storage.UnsupportedFileTypeError):
        storage.extension_for("invoice.exe")


def test_stored_paths_stay_inside_the_root(tmp_path: Path) -> None:
    digest = "a" * 64

    assert storage.document_path(digest, ".pdf", root=tmp_path) == tmp_path.resolve() / (
        f"{digest}.pdf"
    )
    with pytest.raises(ValueError):
        storage.document_path("../../etc/passwd", ".pdf", root=tmp_path)
    with pytest.raises(storage.UnsupportedFileTypeError):
        storage.document_path(digest, "/pdf", root=tmp_path)
    with pytest.raises(storage.PathTraversalError):
        storage.ensure_within(tmp_path / ".." / "outside.pdf", tmp_path)


def test_storage_refuses_empty_content(tmp_path: Path) -> None:
    with pytest.raises(storage.StorageError):
        storage.save_document(
            b"", sha256=hashlib.sha256(b"").hexdigest(), extension=".pdf", root=tmp_path
        )
