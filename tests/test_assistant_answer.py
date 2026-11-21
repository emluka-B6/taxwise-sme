"""Assistant answer tests.

Acceptance criteria covered here:

* a question with covered sources returns an answer with citations, including the source
  version, publication date and fetch date;
* a question without coverage returns the literal refusal ``insufficient sources`` and
  never calls the language model;
* every persisted ``AssistantAnswer`` has ``disclaimer_shown=True`` and the API always
  returns a non-empty disclaimer;
* citations only ever point at allowlisted official domains;
* the provider is swappable via ``ASSISTANT_LLM`` and defaults to the deterministic stub;
* the endpoint rate-limits per client and logs no question content.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import assistant as assistant_api
from app.assistant import answer, retriever
from app.assistant.ingest import is_allowed_host
from app.db.models import AssistantAnswer, KnowledgeChunk, KnowledgeSource
from app.domain.enums import KnowledgePublisher
from app.settings import Settings, get_settings

VAT_URL = "https://www.podatki.gov.pl/vat/stawki-vat-2026/"
ZUS_URL = "https://www.zus.pl/skladki/"
FOREIGN_URL = "https://example.com/porada-vat/"

VAT_QUESTION = "Jakie są stawki VAT w 2026 roku?"
NO_COVERAGE_QUESTION = "Ile wynosi opłata za wydobycie kryptowalut?"

VAT_TEXT = "Podstawowa stawka VAT wynosi 23% i dotyczy większości dostaw towarów i usług."
ZUS_TEXT = "Składka ZUS na ubezpieczenie społeczne zależy od podstawy wymiaru."

VAT_VECTOR = [1.0, 0.0, 0.0]
ZUS_VECTOR = [0.0, 1.0, 0.0]
UNRELATED_VECTOR = [0.0, 0.0, 1.0]


class TopicEmbedder:
    """Deterministic three-dimensional embedder: VAT, ZUS and everything else.

    Real semantic similarity is replaced by one-hot topic vectors so threshold and
    ordering assertions are exact instead of statistical.
    """

    dimension = 3

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if "vat" in lowered:
                vectors.append(list(VAT_VECTOR))
            elif "zus" in lowered or "składk" in lowered:
                vectors.append(list(ZUS_VECTOR))
            else:
                vectors.append(list(UNRELATED_VECTOR))
        return vectors


class CountingAnswerer:
    """Answerer that must not be called (used to prove the refusal short-circuits)."""

    name = "counting-stub"

    def __init__(self) -> None:
        self.calls = 0

    def answer(
        self, question: str, context: Sequence[retriever.RetrievedChunk]
    ) -> answer.AssistantReply:
        self.calls += 1
        raise AssertionError("the model must not be called without retrieved sources")


class FixedAnswerer:
    """Answerer returning a canned answer, to exercise the citation contract."""

    name = "fixed-stub"

    def __init__(self, text: str) -> None:
        self.text = text

    def answer(
        self, question: str, context: Sequence[retriever.RetrievedChunk]
    ) -> answer.AssistantReply:
        return answer.AssistantReply(
            answer=self.text,
            citations=(),
            model=self.name,
            disclaimer=answer.DISCLAIMER,
        )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "assistant_min_score": 0.5,
        "assistant_embedder": "hashing",
        "assistant_llm": "stub",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _seed_source(
    session: Session,
    *,
    url: str = VAT_URL,
    title: str = "Stawki VAT w 2026 roku",
    published_at: datetime | None = datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
    fetched_at: datetime = datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
    version: str | None = "2",
    chunks: Sequence[tuple[str, Sequence[float]]] = ((VAT_TEXT, VAT_VECTOR),),
) -> KnowledgeSource:
    publisher = KnowledgePublisher.MF if "podatki.gov.pl" in url else KnowledgePublisher.OTHER
    source = KnowledgeSource(
        publisher=publisher,
        url=url,
        title=title,
        published_at=published_at,
        fetched_at=fetched_at,
        sha256=hashlib.sha256(f"{url}|{version}".encode()).hexdigest(),
        version=version,
    )
    session.add(source)
    session.flush()
    source.chunks = [
        KnowledgeChunk(ordinal=ordinal, text=text, embedding=[float(value) for value in vector])
        for ordinal, (text, vector) in enumerate(chunks)
    ]
    session.commit()
    return source


@pytest.fixture(autouse=True)
def _clear_assistant_caches() -> Iterator[None]:
    """Process-wide providers and limiters must not leak between tests."""
    assistant_api.get_embedder.cache_clear()
    assistant_api.get_answerer.cache_clear()
    assistant_api.get_rate_limiter.cache_clear()
    yield
    assistant_api.get_embedder.cache_clear()
    assistant_api.get_answerer.cache_clear()
    assistant_api.get_rate_limiter.cache_clear()


# ------------------------------------------------------------------ retrieval


def test_cosine_similarity_handles_orthogonal_and_zero_vectors() -> None:
    assert retriever.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert retriever.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert retriever.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    with pytest.raises(ValueError):
        retriever.cosine_similarity([1.0], [1.0, 0.0])


def test_retrieval_selects_top_k_above_the_threshold(session: Session) -> None:
    source = _seed_source(
        session,
        chunks=((VAT_TEXT, VAT_VECTOR), (ZUS_TEXT, ZUS_VECTOR)),
    )

    matches = retriever.retrieve(
        session,
        VAT_QUESTION,
        embedder=TopicEmbedder(),
        min_score=0.5,
        top_k=5,
    )

    assert [match.source_id for match in matches] == [source.id]
    assert [match.text for match in matches] == [VAT_TEXT]
    assert matches[0].score == pytest.approx(1.0)
    assert matches[0].url == VAT_URL

    all_chunks = retriever.retrieve(
        session,
        VAT_QUESTION,
        embedder=TopicEmbedder(),
        min_score=0.0,
        top_k=5,
    )
    assert [match.text for match in all_chunks] == [VAT_TEXT, ZUS_TEXT]
    assert all_chunks[0].score >= all_chunks[1].score

    limited = retriever.retrieve(
        session,
        VAT_QUESTION,
        embedder=TopicEmbedder(),
        min_score=0.0,
        top_k=1,
    )
    assert [match.text for match in limited] == [VAT_TEXT]


def test_retrieval_skips_sources_outside_the_allowlist(session: Session) -> None:
    _seed_source(session, url=FOREIGN_URL)

    assert (
        retriever.retrieve(
            session,
            VAT_QUESTION,
            embedder=TopicEmbedder(),
            min_score=0.0,
            top_k=5,
        )
        == []
    )


def test_retrieval_rejects_invalid_thresholds(session: Session) -> None:
    with pytest.raises(ValueError):
        retriever.retrieve(session, VAT_QUESTION, embedder=TopicEmbedder(), min_score=1.5)
    with pytest.raises(ValueError):
        retriever.retrieve(
            session, VAT_QUESTION, embedder=TopicEmbedder(), min_score=0.0, top_k=0
        )


# ------------------------------------------------------------------- answers


def test_question_with_coverage_returns_citations_and_source_version(
    session: Session,
) -> None:
    source = _seed_source(session, version="7")

    reply = answer.ask(
        session,
        VAT_QUESTION,
        settings=_settings(),
        answerer=answer.StubAnswerer(),
        embedder=TopicEmbedder(),
    )

    assert reply.refused is False
    assert f"[{source.id}]" in reply.answer
    assert len(reply.citations) == 1
    citation = reply.citations[0]
    assert citation.source_id == source.id
    assert citation.url == VAT_URL
    assert citation.title == "Stawki VAT w 2026 roku"
    assert citation.version == "7"
    assert citation.published_at is not None
    assert citation.fetched_at is not None

    row = session.scalars(select(AssistantAnswer)).one()
    assert row.question == VAT_QUESTION
    assert row.citations == [citation.as_dict()]
    assert row.citations[0]["version"] == "7"
    assert row.disclaimer_shown is True
    assert row.model == "stub"


def test_question_without_coverage_is_refused_without_calling_the_model(
    session: Session,
) -> None:
    _seed_source(session)
    counting = CountingAnswerer()

    reply = answer.ask(
        session,
        NO_COVERAGE_QUESTION,
        settings=_settings(),
        answerer=counting,
        embedder=TopicEmbedder(),
    )

    assert counting.calls == 0
    assert reply.refused is True
    assert reply.answer == answer.INSUFFICIENT_SOURCES_ANSWER
    assert reply.citations == ()

    row = session.scalars(select(AssistantAnswer)).one()
    assert row.answer == "insufficient sources"
    assert row.citations == []
    assert row.disclaimer_shown is True


@pytest.mark.parametrize("question", [VAT_QUESTION, NO_COVERAGE_QUESTION])
def test_every_answer_has_a_disclaimer(session: Session, question: str) -> None:
    _seed_source(session)

    reply = answer.ask(
        session,
        question,
        settings=_settings(),
        answerer=answer.StubAnswerer(),
        embedder=TopicEmbedder(),
    )

    assert reply.disclaimer == answer.DISCLAIMER
    assert reply.disclaimer.strip()
    row = session.scalars(select(AssistantAnswer)).one()
    assert row.disclaimer_shown is True


def test_citations_only_point_at_allowlisted_domains(session: Session) -> None:
    allowed = _seed_source(session)
    _seed_source(session, url=FOREIGN_URL, title="Blog podatkowy")

    reply = answer.ask(
        session,
        VAT_QUESTION,
        settings=_settings(),
        answerer=answer.StubAnswerer(),
        embedder=TopicEmbedder(),
    )

    assert [citation.source_id for citation in reply.citations] == [allowed.id]
    for citation in reply.citations:
        host = urlsplit(citation.url).hostname or ""
        assert is_allowed_host(host), citation.url
    row = session.scalars(select(AssistantAnswer)).one()
    assert all(item["url"] == VAT_URL for item in row.citations or ())


def test_only_disallowed_sources_lead_to_a_refusal(session: Session) -> None:
    _seed_source(session, url=FOREIGN_URL)

    reply = answer.ask(
        session,
        VAT_QUESTION,
        settings=_settings(),
        answerer=answer.StubAnswerer(),
        embedder=TopicEmbedder(),
    )

    assert reply.answer == answer.INSUFFICIENT_SOURCES_ANSWER
    assert reply.citations == ()



# -------------------------------------------------------------- answer contract


@pytest.mark.parametrize("text", ["Odpowiedź bez cytatu", "Odpowiedź [999]"])
def test_answers_outside_the_citation_contract_are_rejected(
    session: Session, text: str
) -> None:
    _seed_source(session)

    with pytest.raises(answer.LLMContractError):
        answer.ask(
            session,
            VAT_QUESTION,
            settings=_settings(),
            answerer=FixedAnswerer(text),
            embedder=TopicEmbedder(),
        )

    assert session.scalar(select(func.count()).select_from(AssistantAnswer)) == 0


def test_model_refusal_is_normalised_and_has_no_citations(session: Session) -> None:
    _seed_source(session)

    reply = answer.ask(
        session,
        VAT_QUESTION,
        settings=_settings(),
        answerer=FixedAnswerer("insufficient sources."),
        embedder=TopicEmbedder(),
    )

    assert reply.refused is True
    assert reply.answer == answer.INSUFFICIENT_SOURCES_ANSWER
    assert reply.citations == ()
    row = session.scalars(select(AssistantAnswer)).one()
    assert row.citations == []
    assert row.disclaimer_shown is True


def test_prompt_requires_citation_format_publication_date_and_version(
    session: Session,
) -> None:
    source = _seed_source(session, version="4")
    context = retriever.retrieve(
        session,
        VAT_QUESTION,
        embedder=TopicEmbedder(),
        min_score=0.5,
    )

    prompt = answer.build_prompt(VAT_QUESTION, context)

    assert "[source_id]" in prompt
    assert f"[source_id={source.id}]" in prompt
    assert "published_at=2026-01-15" in prompt
    assert "version=4" in prompt
    assert answer.INSUFFICIENT_SOURCES_ANSWER in prompt
    assert VAT_QUESTION in prompt


def test_llm_backend_is_swappable_via_the_environment() -> None:
    assert isinstance(answer.build_answerer(_settings()), answer.StubAnswerer)

    configured = _settings(
        assistant_llm="openai",
        assistant_llm_model="gpt-test",
        assistant_llm_api_key="not-a-real-secret",
    )
    provider = answer.build_answerer(configured)
    assert isinstance(provider, answer.OpenAICompatibleAnswerer)
    assert provider.name == "gpt-test"

    with pytest.raises(answer.LLMNotConfigured):
        answer.build_answerer(_settings(assistant_llm="openai"))

    with pytest.raises(answer.LLMNotConfigured):
        answer.build_answerer(_settings(assistant_llm="mystery"))


def test_min_score_reads_the_environment_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_MIN_SCORE", "0.75")
    assert Settings(_env_file=None).assistant_min_score == pytest.approx(0.75)

    monkeypatch.setenv("ASSISTANT_MIN_SCORE", "1.5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)



# ----------------------------------------------------------------------- API


def _configure_client(
    client: TestClient,
    *,
    embedder: object | None = None,
    answerer: object | None = None,
) -> None:
    client.app.dependency_overrides[assistant_api.get_embedder] = (
        lambda: embedder or TopicEmbedder()
    )
    client.app.dependency_overrides[assistant_api.get_answerer] = (
        lambda: answerer or answer.StubAnswerer()
    )


def test_api_returns_answer_citations_and_disclaimer(
    api_client: TestClient, session: Session
) -> None:
    source = _seed_source(session, version="3")
    _configure_client(api_client)

    response = api_client.post("/api/assistant/ask", json={"question": VAT_QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert f"[{source.id}]" in body["answer"]
    assert body["disclaimer"] == answer.DISCLAIMER
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["source_id"] == source.id
    assert citation["url"] == VAT_URL
    assert citation["title"] == "Stawki VAT w 2026 roku"
    assert citation["published_at"] is not None
    assert citation["fetched_at"] is not None
    assert citation["version"] == "3"
    host = urlsplit(citation["url"]).hostname or ""
    assert is_allowed_host(host)

    row = session.scalars(select(AssistantAnswer)).one()
    assert row.disclaimer_shown is True
    stored = row.citations or []
    assert [item["url"] for item in stored] == [citation["url"]]
    assert stored[0]["version"] == "3"
    assert stored[0]["title"] == citation["title"]


def test_api_refusal_has_no_citations_but_always_a_disclaimer(
    api_client: TestClient, session: Session
) -> None:
    _seed_source(session)
    counting = CountingAnswerer()
    _configure_client(api_client, answerer=counting)

    response = api_client.post(
        "/api/assistant/ask", json={"question": NO_COVERAGE_QUESTION}
    )

    assert response.status_code == 200
    body = response.json()
    assert counting.calls == 0
    assert body["answer"] == "insufficient sources"
    assert body["citations"] == []
    assert body["disclaimer"] == answer.DISCLAIMER

    row = session.scalars(select(AssistantAnswer)).one()
    assert row.answer == "insufficient sources"
    assert row.citations == []
    assert row.disclaimer_shown is True


def test_api_rejects_an_empty_question(api_client: TestClient) -> None:
    _configure_client(api_client)

    empty = api_client.post("/api/assistant/ask", json={"question": ""})
    blank = api_client.post("/api/assistant/ask", json={"question": "   "})

    assert empty.status_code == 422
    assert blank.status_code == 422


def test_api_rate_limits_per_client(api_client: TestClient) -> None:
    limiter = assistant_api.RateLimiter(limit=2)
    api_client.app.dependency_overrides[assistant_api.get_rate_limiter] = lambda: limiter
    _configure_client(api_client)

    for _ in range(2):
        response = api_client.post("/api/assistant/ask", json={"question": VAT_QUESTION})
        assert response.status_code == 200

    limited = api_client.post("/api/assistant/ask", json={"question": VAT_QUESTION})

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_api_logs_no_question_content(
    api_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _configure_client(api_client)

    with caplog.at_level(logging.INFO):
        response = api_client.post(
            "/api/assistant/ask", json={"question": NO_COVERAGE_QUESTION}
        )

    assert response.status_code == 200
    assert NO_COVERAGE_QUESTION not in caplog.text
    assert "kryptowalut" not in caplog.text
    assert "question_chars=" in caplog.text


def test_api_reports_misconfigured_llm_as_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASSISTANT_LLM", "openai")
    monkeypatch.delenv("ASSISTANT_LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    assistant_api.get_answerer.cache_clear()
    api_client.app.dependency_overrides[assistant_api.get_embedder] = lambda: TopicEmbedder()

    response = api_client.post("/api/assistant/ask", json={"question": VAT_QUESTION})

    assert response.status_code == 503
    assert "ASSISTANT_LLM_API_KEY" in response.json()["detail"]


def test_api_endpoint_is_documented(api_client: TestClient) -> None:
    schema = api_client.get("/openapi.json").json()

    assert "/api/assistant/ask" in schema["paths"]
    assert "post" in schema["paths"]["/api/assistant/ask"]
