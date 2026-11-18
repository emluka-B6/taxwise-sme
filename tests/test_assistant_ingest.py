"""Knowledge-base ingestion tests.

Acceptance criteria covered here: an HTML fixture from an allowlisted domain becomes a
``KnowledgeSource`` with embedded ``KnowledgeChunk`` rows; changed content creates a new
version while the previous version and its chunks survive; re-ingesting identical content
creates no duplicate; and URLs outside the allowlist (including redirect targets and
lookalike hosts) are rejected before any fetch happens.
"""

from __future__ import annotations

import importlib.util
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant import embedder, ingest
from app.db.models import KnowledgeChunk, KnowledgeSource
from app.domain.enums import KnowledgePublisher

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"

VAT_URL = "https://www.podatki.gov.pl/vat/stawki-vat-2026/"

#: URLs that must never reach the fetcher.
DISALLOWED_URLS = [
    "https://example.com/poradnik",
    "https://podatki.gov.pl.evil.example/vat",
    "https://evil-podatki.gov.com/vat",
    "https://www.zus.pl.evil.example/skladki",
    "https://evil.example/?next=https://www.zus.pl",
    "http://www.zus.pl/skladki",
    "https://user:secret@www.zus.pl/skladki",
    "ftp://www.zus.pl/skladki",
]

#: Official hosts (with subdomains) that must pass the allowlist.
ALLOWED_URLS = [
    "https://podatki.gov.pl/vat",
    "https://www.podatki.gov.pl/vat",
    "https://zus.pl/skladki",
    "https://www.zus.pl/skladki",
    "https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU2026000001",
    "https://www.gov.pl/web/finanse/vat",
]


class _FakeFetcher:
    """Offline fetcher returning one canned response and recording the requested URLs."""

    def __init__(
        self,
        body: bytes,
        *,
        final_url: str | None = None,
        content_type: str | None = "text/html; charset=utf-8",
    ) -> None:
        self.body = body
        self.final_url = final_url
        self.content_type = content_type
        self.calls: list[str] = []

    def __call__(self, url: str) -> ingest.FetchedPage:
        self.calls.append(url)
        return ingest.FetchedPage(
            final_url=self.final_url or url,
            content_type=self.content_type,
            body=self.body,
            charset="utf-8",
        )


class _CountingEmbedder:
    """Hashing embedder that records how many batches were embedded."""

    def __init__(self, dimension: int = 32) -> None:
        self._inner = embedder.HashingEmbedder(dimension=dimension)
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return self._inner.embed(texts)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _embedder() -> embedder.HashingEmbedder:
    return embedder.HashingEmbedder(dimension=32)


def _chunks(session: Session, source_id: int) -> list[KnowledgeChunk]:
    return list(
        session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.source_id == source_id)
            .order_by(KnowledgeChunk.ordinal)
        )
    )


def _sources(session: Session) -> list[KnowledgeSource]:
    return list(session.scalars(select(KnowledgeSource).order_by(KnowledgeSource.id)))


# --------------------------------------------------------------------------- ingest


def test_ingest_fixture_creates_source_and_chunks(session: Session) -> None:
    fetcher = _FakeFetcher(_fixture("podatki_vat_2026.html"), final_url=VAT_URL)
    fetched_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    result = ingest.ingest_url(
        session, VAT_URL, embedder=_embedder(), fetcher=fetcher, fetched_at=fetched_at
    )
    session.commit()

    assert fetcher.calls == [VAT_URL]
    assert result.created is True
    source = result.source
    assert source.id is not None
    assert source.url == VAT_URL
    assert source.publisher is KnowledgePublisher.MF
    assert source.title == "Stawki VAT w 2026 roku"
    assert source.version == "1"
    assert len(source.sha256) == 64
    assert all(character in "0123456789abcdef" for character in source.sha256)
    assert source.fetched_at is not None
    assert (source.fetched_at.year, source.fetched_at.month, source.fetched_at.day) == (
        2026,
        5,
        1,
    )
    assert source.published_at is not None
    assert (source.published_at.year, source.published_at.month, source.published_at.day) == (
        2026,
        1,
        15,
    )

    chunks = _chunks(session, source.id)
    assert result.chunk_count == len(chunks) >= 2
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.text and len(chunk.text) <= ingest.CHUNK_MAX_CHARS for chunk in chunks)
    assert chunks[0].text.startswith("Stawki VAT w 2026 roku")
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "23%" in joined
    assert "wiążącej informacji stawkowej" in joined
    # Navigation, scripts, asides and the footer are not part of the knowledge base.
    assert "dataLayer" not in joined
    assert "Zobacz także" not in joined
    assert "Serwis informacyjny" not in joined
    # Consecutive chunks overlap, so a sentence split at a boundary is still retrievable.
    assert chunks[1].text[:60] in chunks[0].text
    # Every chunk carries a vector of the embedder's width, persisted as JSON text.
    assert all(chunk.embedding is not None for chunk in chunks)
    assert all(len(chunk.embedding or []) == 32 for chunk in chunks)
    assert all(any(value != 0.0 for value in chunk.embedding or []) for chunk in chunks)
    raw = (
        session.connection()
        .exec_driver_sql("SELECT embedding FROM knowledge_chunks WHERE source_id = ?", (source.id,))
        .scalar()
    )
    assert isinstance(raw, str)
    assert raw.startswith("[")


def test_changed_content_creates_next_version_and_keeps_previous(session: Session) -> None:
    first = ingest.ingest_url(
        session,
        VAT_URL,
        embedder=_embedder(),
        fetcher=_FakeFetcher(_fixture("podatki_vat_2026.html")),
    )
    session.commit()
    first_id = first.source.id
    first_chunks = {chunk.id: chunk.text for chunk in _chunks(session, first_id)}
    first_digest = first.source.sha256

    second = ingest.ingest_url(
        session,
        VAT_URL,
        embedder=_embedder(),
        fetcher=_FakeFetcher(_fixture("podatki_vat_2026_updated.html")),
    )
    session.commit()

    assert second.created is True
    assert second.source.id != first_id
    rows = _sources(session)
    assert [row.version for row in rows] == ["1", "2"]
    assert rows[0].url == rows[1].url == VAT_URL
    assert rows[0].sha256 == first_digest
    assert rows[1].sha256 != first_digest
    assert len(rows[1].sha256) == 64

    # The previous version is append-only: neither its chunks nor its text changed.
    preserved = {chunk.id: chunk.text for chunk in _chunks(session, first_id)}
    assert preserved == first_chunks
    assert "1 kwietnia 2026" not in "\n".join(first_chunks.values())
    assert "1 kwietnia 2026" in "\n".join(
        chunk.text for chunk in _chunks(session, second.source.id)
    )


def test_unchanged_content_is_not_indexed_twice(session: Session) -> None:
    embedding = _CountingEmbedder()
    body = _fixture("podatki_vat_2026.html")
    first = ingest.ingest_url(session, VAT_URL, embedder=embedding, fetcher=_FakeFetcher(body))
    session.commit()
    chunk_ids = [chunk.id for chunk in _chunks(session, first.source.id)]

    second = ingest.ingest_url(session, VAT_URL, embedder=embedding, fetcher=_FakeFetcher(body))
    session.commit()

    assert first.created is True
    assert second.created is False
    assert second.source.id == first.source.id
    assert second.source.version == "1"
    assert second.chunk_count == first.chunk_count
    assert embedding.calls == 1  # unchanged content is not embedded again
    assert session.scalar(select(func.count()).select_from(KnowledgeSource)) == 1
    assert [chunk.id for chunk in _chunks(session, first.source.id)] == chunk_ids


def test_same_digest_under_another_url_is_not_duplicated(session: Session) -> None:
    body = _fixture("podatki_vat_2026.html")
    first = ingest.ingest_url(session, VAT_URL, embedder=_embedder(), fetcher=_FakeFetcher(body))
    session.commit()
    second = ingest.ingest_url(
        session,
        "https://podatki.gov.pl/vat/stawki-vat-2026/",
        embedder=_embedder(),
        fetcher=_FakeFetcher(body),
    )
    session.commit()

    assert second.created is False
    assert second.source.id == first.source.id
    assert session.scalar(select(func.count()).select_from(KnowledgeSource)) == 1


# --------------------------------------------------------------------------- allowlist


def test_allowlist_accepts_official_domains_and_subdomains() -> None:
    for url in ALLOWED_URLS:
        assert ingest.validate_url(url).startswith("https://")


def test_validate_url_canonicalises_host_case_and_drops_fragments() -> None:
    assert ingest.validate_url("HTTPS://WWW.ZUS.PL/skladki#tabela") == (
        "https://www.zus.pl/skladki"
    )
    assert ingest.validate_url("https://www.zus.pl./skladki") == "https://www.zus.pl/skladki"


def test_publisher_is_derived_from_the_host() -> None:
    assert ingest.publisher_for_url("https://www.podatki.gov.pl/vat") is KnowledgePublisher.MF
    assert ingest.publisher_for_url("https://www.zus.pl/skladki") is KnowledgePublisher.ZUS
    assert (
        ingest.publisher_for_url("https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp")
        is KnowledgePublisher.ISAP
    )
    assert ingest.publisher_for_url("https://www.gov.pl/web/finanse") is KnowledgePublisher.OTHER


@pytest.mark.parametrize("url", DISALLOWED_URLS)
def test_url_outside_allowlist_is_rejected_before_fetch(session: Session, url: str) -> None:
    fetcher = _FakeFetcher(_fixture("podatki_vat_2026.html"))
    with pytest.raises(ingest.DomainNotAllowed):
        ingest.ingest_url(session, url, embedder=_embedder(), fetcher=fetcher)
    assert fetcher.calls == []
    assert _sources(session) == []
    assert session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0


def test_redirect_to_a_disallowed_host_is_rejected(session: Session) -> None:
    fetcher = _FakeFetcher(
        _fixture("podatki_vat_2026.html"), final_url="https://evil.example/landing"
    )
    with pytest.raises(ingest.DomainNotAllowed):
        ingest.ingest_url(session, VAT_URL, embedder=_embedder(), fetcher=fetcher)
    assert fetcher.calls == [VAT_URL]
    assert _sources(session) == []


def test_redirect_within_the_allowlist_stores_the_final_url(session: Session) -> None:
    final_url = "https://podatki.gov.pl/vat/stawki-vat-2026/"
    fetcher = _FakeFetcher(_fixture("podatki_vat_2026.html"), final_url=final_url)

    result = ingest.ingest_url(session, VAT_URL, embedder=_embedder(), fetcher=fetcher)
    session.commit()

    assert result.source.url == final_url
    assert result.source.publisher is KnowledgePublisher.MF


def test_non_html_response_is_rejected(session: Session) -> None:
    fetcher = _FakeFetcher(b"%PDF-1.7\n%%EOF\n", content_type="application/pdf")
    with pytest.raises(ingest.ContentError):
        ingest.ingest_url(session, VAT_URL, embedder=_embedder(), fetcher=fetcher)
    assert _sources(session) == []


# --------------------------------------------------------------------------- chunking


def test_chunking_respects_size_bounds_and_overlaps() -> None:
    sentence = "Składka zdrowotna jest obliczana od podstawy wymiaru. "
    text = "\n\n".join(f"Akapit {index}. {sentence * 12}" for index in range(5))

    chunks = ingest.chunk_text(text)

    assert len(chunks) >= 2
    assert all(0 < len(chunk) <= ingest.CHUNK_MAX_CHARS for chunk in chunks)
    assert all(chunk == chunk.strip() for chunk in chunks)
    assert chunks[1][:60] in chunks[0]
    assert ingest.chunk_text("  \n\n  ") == []


def test_chunking_rejects_impossible_overlap() -> None:
    with pytest.raises(ValueError):
        ingest.chunk_text("tekst", max_chars=10, overlap=10)
    with pytest.raises(ValueError):
        ingest.chunk_text("tekst", max_chars=0)


# --------------------------------------------------------------------------- embedder


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    first = embedder.HashingEmbedder(dimension=64)
    second = embedder.HashingEmbedder(dimension=64)
    texts = ["Składka zdrowotna 2026", "Podatek VAT od usług"]

    vectors = first.embed(texts)

    assert vectors == second.embed(texts)
    assert vectors[0] == first.embed([texts[0]])[0]
    assert len(vectors) == 2
    assert all(len(vector) == 64 for vector in vectors)
    assert math.isclose(math.sqrt(sum(value * value for value in vectors[0])), 1.0)
    assert vectors[0] != vectors[1]


def test_hashing_embedder_returns_zero_vector_for_empty_text() -> None:
    assert embedder.HashingEmbedder(dimension=8).embed(["", "  "]) == [
        [0.0] * 8,
        [0.0] * 8,
    ]


def test_sentence_transformer_embedder_requires_the_optional_extra() -> None:
    if importlib.util.find_spec("sentence_transformers") is not None:
        pytest.skip("sentence-transformers is installed in this environment")
    with pytest.raises(embedder.EmbedderUnavailable):
        embedder.SentenceTransformerEmbedder().embed(["test"])
