"""Smoke tests of the minimal review UI (server-rendered Jinja2 + HTMX).

Acceptance criteria covered here: ``/documents``, ``/deadlines`` and ``/assistant`` answer
``200``; the assistant page always shows the disclaimer; the document register lists
persisted rows; a deadline status change sent the way HTMX sends it updates the stored
record; and template values are HTML-escaped because autoescaping is enabled.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import assistant as assistant_api
from app.api import ui as ui_api
from app.assistant import answer
from app.db.models import Deadline, Document, KnowledgeChunk, KnowledgeSource
from app.domain.enums import (
    DeadlineKind,
    DeadlineStatus,
    DocumentSource,
    DocumentStatus,
    DocumentType,
    KnowledgePublisher,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_assistant_state() -> Iterator[None]:
    """Cached providers and the rate limiter must not leak between tests."""
    assistant_api.get_embedder.cache_clear()
    assistant_api.get_answerer.cache_clear()
    assistant_api.get_rate_limiter.cache_clear()
    yield
    assistant_api.get_embedder.cache_clear()
    assistant_api.get_answerer.cache_clear()
    assistant_api.get_rate_limiter.cache_clear()


@pytest.fixture
def document(session: Session) -> Document:
    """One classified cost invoice, as the register page should display it."""
    row = Document(
        type=DocumentType.VAT_INVOICE_COST,
        issue_date=date(2026, 3, 2),
        period="2026-03",
        amount_gross=Decimal("1234.56"),
        source=DocumentSource.UPLOAD,
        file_path="faktura_2026_03.pdf",
        sha256="a" * 64,
        status=DocumentStatus.CLASSIFIED,
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def deadline(session: Session) -> Deadline:
    """One open VAT declaration that the UI can mark done."""
    row = Deadline(
        kind=DeadlineKind.VAT_DECLARATION,
        period="2026-03",
        due_date=date(2026, 4, 25),
        status=DeadlineStatus.OPEN,
        notes="deklaracja VAT",
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def documents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point storage at a per-test directory; the UI test never writes into the repo."""
    directory = tmp_path / "documents"
    monkeypatch.setenv("DOCUMENTS_DIR", str(directory))
    get_settings.cache_clear()
    yield directory
    get_settings.cache_clear()


class _TopicEmbedder:
    """Deterministic two-dimensional embedder; the seeded chunk always matches."""

    dimension = 2

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def knowledge_source(session: Session) -> KnowledgeSource:
    """One allowlisted official source so the assistant answer has a citation."""
    source = KnowledgeSource(
        publisher=KnowledgePublisher.MF,
        url="https://www.podatki.gov.pl/vat/stawki-vat-2026/",
        title="Stawki VAT w 2026 roku",
        published_at=datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
        fetched_at=datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
        sha256=hashlib.sha256(b"vat-2026").hexdigest(),
        version="3",
    )
    session.add(source)
    session.flush()
    source.chunks = [
        KnowledgeChunk(
            ordinal=0,
            text="Podstawowa stawka VAT wynosi 23% i dotyczy większości dostaw.",
            embedding=[1.0, 0.0],
        )
    ]
    session.commit()
    return source


# --------------------------------------------------------------------------- pages


def test_documents_page_lists_fixture_records(
    api_client: TestClient, document: Document
) -> None:
    response = api_client.get("/documents")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Dokumenty" in response.text
    assert document.file_path is not None
    assert document.file_path in response.text
    assert "1234.56" in response.text
    assert DocumentType.VAT_INVOICE_COST.value in response.text


def test_documents_page_filters_by_type(api_client: TestClient, document: Document) -> None:
    matching = api_client.get("/documents", params={"type": "VAT_INVOICE_COST"})
    other = api_client.get("/documents", params={"type": "ZUS"})
    all_documents = api_client.get("/documents", params={"type": "", "status": ""})
    unknown = api_client.get("/documents", params={"type": "UNKNOWN"})

    assert matching.status_code == 200
    assert "faktura_2026_03.pdf" in matching.text
    assert other.status_code == 200
    assert "faktura_2026_03.pdf" not in other.text
    assert "Brak dokumentów." in other.text
    # The 'all' option submits empty values; they must not be treated as enum errors.
    assert all_documents.status_code == 200
    assert "faktura_2026_03.pdf" in all_documents.text
    assert unknown.status_code == 200
    assert "faktura_2026_03.pdf" in unknown.text


def test_deadlines_page_renders_registered_deadlines(
    api_client: TestClient, deadline: Deadline
) -> None:
    response = api_client.get("/deadlines")
    unfiltered = api_client.get("/deadlines", params={"status": ""})

    assert response.status_code == 200
    assert "Terminy" in response.text
    assert deadline.period in response.text
    assert DeadlineKind.VAT_DECLARATION.value in response.text
    assert unfiltered.status_code == 200
    assert deadline.period in unfiltered.text


def test_assistant_page_shows_the_disclaimer(api_client: TestClient) -> None:
    response = api_client.get("/assistant")

    assert response.status_code == 200
    assert answer.DISCLAIMER in response.text
    assert 'id="assistant-disclaimer"' in response.text


def test_assistant_answer_carries_disclaimer_and_citations_section(
    api_client: TestClient,
) -> None:
    response = api_client.post(
        "/assistant", data={"question": "Jakie są stawki VAT w 2026 roku?"}
    )

    assert response.status_code == 200
    assert answer.INSUFFICIENT_SOURCES_ANSWER in response.text
    assert answer.DISCLAIMER in response.text
    # No knowledge source is seeded, so the refusal is an empty, citation-less answer.
    assert "Odpowiedź nie cytuje żadnego źródła." in response.text


def test_assistant_answer_renders_citations_next_to_the_disclaimer(
    api_client: TestClient, knowledge_source: KnowledgeSource
) -> None:
    api_client.app.dependency_overrides[assistant_api.get_embedder] = _TopicEmbedder

    response = api_client.post(
        "/assistant", data={"question": "Jakie są stawki VAT w 2026 roku?"}
    )

    assert response.status_code == 200
    assert "23%" in response.text
    assert knowledge_source.title in response.text
    assert knowledge_source.url in response.text
    assert "wersja: 3" in response.text
    assert answer.DISCLAIMER in response.text
    assert answer.INSUFFICIENT_SOURCES_ANSWER not in response.text


# --------------------------------------------------------------------------- HTMX


def test_htmx_deadline_status_change_updates_the_record(
    api_client: TestClient, session: Session, deadline: Deadline
) -> None:
    response = api_client.patch(
        f"/deadlines/{deadline.id}",
        data={"status": DeadlineStatus.DONE.value},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "<td>DONE</td>" in response.text
    # HTMX receives a fragment, not a full layout.
    assert "<html" not in response.text.lower()

    session.refresh(deadline)
    assert deadline.status is DeadlineStatus.DONE


def test_upload_form_delegates_to_the_documents_endpoint(
    api_client: TestClient, session: Session, documents_dir: Path
) -> None:
    response = api_client.post(
        "/documents",
        files={
            "file": (
                "faktura_2026_04.pdf",
                b"%PDF-1.7\nFaktura VAT nr 7/2026\n%%EOF\n",
                "application/pdf",
            )
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    rows = list(session.scalars(select(Document)))
    assert len(rows) == 1
    assert rows[0].type is DocumentType.VAT_INVOICE_COST
    assert rows[0].file_path is not None
    assert rows[0].file_path in response.text
    assert (documents_dir / rows[0].file_path).is_file()


# --------------------------------------------------------------------------- escaping


def test_autoescape_is_enabled_for_templates() -> None:
    assert ui_api.templates.env.autoescape is True


def test_document_values_are_escaped(api_client: TestClient, session: Session) -> None:
    payload = "<script>alert('x')</script>.pdf"
    session.add(
        Document(
            type=DocumentType.OTHER,
            issue_date=date(2026, 3, 2),
            period="2026-03",
            amount_gross=Decimal("0.00"),
            source=DocumentSource.UPLOAD,
            file_path=payload,
            sha256="b" * 64,
            status=DocumentStatus.CLASSIFIED,
        )
    )
    session.commit()

    response = api_client.get("/documents")

    assert response.status_code == 200
    assert payload not in response.text
    assert "&lt;script&gt;alert(" in response.text
