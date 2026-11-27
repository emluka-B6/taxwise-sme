"""Grounded answer generation with citations, publication dates and a disclaimer.

Flow of :func:`ask`:

1. retrieve chunks above ``ASSISTANT_MIN_SCORE`` (see :mod:`app.assistant.retriever`);
2. when nothing is above the threshold, return :data:`INSUFFICIENT_SOURCES_ANSWER`
   **without calling the model at all** - there is nothing to summarise and a model must
   not fill the gap from parametric knowledge;
3. otherwise call the configured :class:`~app.assistant.base.Answerer`, then re-derive
   citations from the ``[source_id]`` markers in the text against the retrieved chunks
   (unknown ids and non-allowlisted URLs raise :class:`LLMContractError`);
4. persist one ``assistant_answers`` row with the citations and ``disclaimer_shown=True``.

The LLM backend is selected by ``ASSISTANT_LLM``. The default ``stub`` is deterministic
and needs no network, which is what the test suite uses; ``openai`` talks to any
OpenAI-compatible chat endpoint and reads its key from ``ASSISTANT_LLM_API_KEY`` only.
No secret is stored in the repository.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any, Final

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.assistant.base import Answerer, AssistantReply, Citation
from app.assistant.embedder import Embedder
from app.assistant.ingest import DomainNotAllowed, validate_url
from app.assistant.retriever import RetrievedChunk, retrieve_for_question
from app.db.models import AssistantAnswer
from app.settings import Settings, get_settings

__all__ = [
    "DISCLAIMER",
    "INSUFFICIENT_SOURCES_ANSWER",
    "LLMContractError",
    "LLMError",
    "LLMNotConfigured",
    "OpenAICompatibleAnswerer",
    "StubAnswerer",
    "answer_question",
    "ask",
    "build_answerer",
    "build_prompt",
    "cited_source_ids",
]

logger = logging.getLogger(__name__)

#: Exact refusal text returned when no retrieved chunk reaches the similarity threshold.
INSUFFICIENT_SOURCES_ANSWER: Final[str] = "insufficient sources"

#: Shown with (and persisted alongside) every answer, including refusals.
DISCLAIMER: Final[str] = (
    "Informacje mają charakter informacyjny i nie stanowią porady podatkowej. "
    "Zweryfikuj je w cytowanych źródłach urzędowych."
)

#: Upper bound for a chat completion body; protects the process from a runaway provider.
MAX_LLM_RESPONSE_BYTES: Final[int] = 1024 * 1024

#: ``[12]`` - the only citation format the prompt allows.
_CITATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[(\d+)\]")

INSTRUCTIONS: Final[str] = (
    "Jesteś asystentem podatkowym dla polskich jednoosobowych działalności gospodarczych.\n"
    "Odpowiadasz WYŁĄCZNIE na podstawie poniższych ŹRÓDEŁ i nigdy nie uzupełniasz\n"
    "odpowiedzi własną wiedzą.\n"
    "\n"
    "ZASADY ODPOWIEDZI:\n"
    "1. Każde twierdzenie musi być poparte cytatem w formacie [source_id], np. [12].\n"
    "2. Przy każdym cytacie podaj datę publikacji źródła (published_at).\n"
    "3. Nie wymyślaj adresów URL, identyfikatorów źródeł ani wersji dokumentu.\n"
    "4. Jeżeli źródła nie odpowiadają na pytanie, odpowiedz dokładnie: "
    f"{INSUFFICIENT_SOURCES_ANSWER}\n"
)


class LLMNotConfigured(RuntimeError):
    """The selected LLM backend is unknown or missing its environment configuration."""


class LLMError(RuntimeError):
    """The provider could not be reached or returned an unusable payload."""


class LLMContractError(RuntimeError):
    """The model broke the citation contract (no marker, unknown id or foreign URL)."""


def _published_label(chunk: RetrievedChunk) -> str:
    return chunk.published_at.isoformat() if chunk.published_at else "brak"


def _format_source(chunk: RetrievedChunk) -> str:
    """One prompt block; every field needed to verify a citation is visible to the model."""
    return (
        f"[source_id={chunk.source_id}] "
        f"title={chunk.title} | url={chunk.url} | published_at={_published_label(chunk)} "
        f"| fetched_at={chunk.fetched_at.isoformat()} | version={chunk.version or 'brak'} "
        f"| chunk_ordinal={chunk.ordinal} | similarity={chunk.score:.3f}\n"
        f"{chunk.text}"
    )


def build_prompt(question: str, context: Sequence[RetrievedChunk]) -> str:
    """Render the grounded prompt: instructions, question and versioned source blocks."""
    blocks = "\n\n".join(_format_source(chunk) for chunk in context)
    return (
        f"{INSTRUCTIONS}\n"
        f"PYTANIE:\n{question}\n\n"
        f"ŹRÓDŁA (wersjonowane, urzędowe):\n{blocks}\n"
    )


def cited_source_ids(answer: str) -> list[int]:
    """Source ids referenced as ``[id]`` in ``answer``, in first-appearance order."""
    ordered: list[int] = []
    for match in _CITATION_PATTERN.finditer(answer):
        source_id = int(match.group(1))
        if source_id not in ordered:
            ordered.append(source_id)
    return ordered


def _is_refusal(answer: str) -> bool:
    return answer.strip().rstrip(".!").strip().lower() == INSUFFICIENT_SOURCES_ANSWER


def _summarise(text: str, limit: int = 280) -> str:
    """Deterministic single-paragraph excerpt used by the stub provider."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit].rsplit(" ", 1)[0] or collapsed[:limit]
    return f"{cut}…"


class StubAnswerer:
    """Deterministic, offline provider used by default and by the test suite.

    It quotes the best-scoring chunk, cites its ``source_id`` and repeats the publication
    date, so tests can assert the full citation contract without a model download.
    """

    name: Final[str] = "stub"

    def answer(self, question: str, context: Sequence[RetrievedChunk]) -> AssistantReply:
        if not context:
            return AssistantReply(
                answer=INSUFFICIENT_SOURCES_ANSWER,
                citations=(),
                model=self.name,
                disclaimer=DISCLAIMER,
                refused=True,
            )
        first = context[0]
        published = (
            first.published_at.date().isoformat() if first.published_at else "brak"
        )
        answer = (
            f"{_summarise(first.text)} [{first.source_id}] "
            f"(data publikacji: {published})"
        )
        return AssistantReply(
            answer=answer,
            citations=(first.to_citation(),),
            model=self.name,
            disclaimer=DISCLAIMER,
        )


class OpenAICompatibleAnswerer:
    """Minimal chat-completions client for any OpenAI-compatible endpoint (stdlib only)."""

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str,
        timeout: float = 30.0,
    ) -> None:
        self.name = model
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout

    def answer(self, question: str, context: Sequence[RetrievedChunk]) -> AssistantReply:
        body = json.dumps(
            {
                "model": self.name,
                "temperature": 0,
                "messages": [
                    {"role": "user", "content": build_prompt(question, context)},
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(MAX_LLM_RESPONSE_BYTES)
        except (urllib.error.URLError, OSError) as exc:
            # Never include the request (and thus the key or the question) in the error.
            raise LLMError("LLM request failed") from exc
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned a non-JSON response") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response is missing choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM returned an empty answer")
        return AssistantReply(
            answer=content.strip(),
            citations=(),
            model=self.name,
            disclaimer=DISCLAIMER,
            refused=_is_refusal(content),
        )


def build_answerer(settings: Settings) -> Answerer:
    """Resolve ``ASSISTANT_LLM`` to a provider; the default is the deterministic stub."""
    provider = settings.assistant_llm.strip().lower()
    if provider == "stub":
        return StubAnswerer()
    if provider in {"openai", "openai-compatible", "openai_compatible"}:
        api_key = settings.assistant_llm_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            raise LLMNotConfigured(
                "ASSISTANT_LLM=openai requires ASSISTANT_LLM_API_KEY in the environment"
            )
        return OpenAICompatibleAnswerer(
            model=settings.assistant_llm_model,
            api_key=api_key,
            base_url=settings.assistant_llm_base_url,
            timeout=settings.assistant_llm_timeout_seconds,
        )
    raise LLMNotConfigured(
        f"ASSISTANT_LLM={settings.assistant_llm!r} is not supported; use 'stub' or 'openai'"
    )


def _citations_for(answer: str, context: Sequence[RetrievedChunk]) -> tuple[Citation, ...]:
    """Map every ``[source_id]`` marker to a retrieved, allowlisted source.

    Raises :class:`LLMContractError` when the model produced no marker, cited a source it
    was not given, or somehow produced a citation outside the ingestion allowlist.
    """
    by_source: dict[int, RetrievedChunk] = {}
    for chunk in context:
        by_source.setdefault(chunk.source_id, chunk)
    source_ids = cited_source_ids(answer)
    if not source_ids:
        raise LLMContractError("answer contains no [source_id] citation")
    citations: list[Citation] = []
    for source_id in source_ids:
        retrieved = by_source.get(source_id)
        if retrieved is None:
            raise LLMContractError(f"answer cites unknown source [{source_id}]")
        try:
            validate_url(retrieved.url)
        except DomainNotAllowed as exc:  # defence in depth; retrieval already filters
            raise LLMContractError(f"citation [{source_id}] is outside the allowlist") from exc
        citations.append(retrieved.to_citation())
    return tuple(citations)


def _answerer_name(answerer: Answerer) -> str:
    name = getattr(answerer, "name", None)
    return name if isinstance(name, str) and name else type(answerer).__name__


def _reply_for(
    question: str,
    context: Sequence[RetrievedChunk],
    resolved: Settings,
    answerer: Answerer | None,
) -> AssistantReply:
    """Build the reply: refuse when nothing was retrieved, otherwise use the provider."""
    if not context:
        # No generation: a question without coverage must not reach an LLM.
        model = _answerer_name(answerer) if answerer is not None else resolved.assistant_llm
        return AssistantReply(
            answer=INSUFFICIENT_SOURCES_ANSWER,
            citations=(),
            model=model.strip() or "stub",
            disclaimer=DISCLAIMER,
            refused=True,
        )

    provider = answerer or build_answerer(resolved)
    raw = provider.answer(question, context)
    if _is_refusal(raw.answer):
        return AssistantReply(
            answer=INSUFFICIENT_SOURCES_ANSWER,
            citations=(),
            model=raw.model or provider.name,
            disclaimer=DISCLAIMER,
            refused=True,
        )
    return AssistantReply(
        answer=raw.answer,
        citations=_citations_for(raw.answer, context),
        model=raw.model or provider.name,
        disclaimer=DISCLAIMER,
        refused=False,
    )


def ask(
    session: Session,
    question: str,
    *,
    settings: Settings | None = None,
    answerer: Answerer | None = None,
    embedder: Embedder | None = None,
    min_score: float | None = None,
    top_k: int | None = None,
) -> AssistantReply:
    """Answer ``question`` from the versioned knowledge base and store an audit row.

    The caller owns the transaction: the row is flushed (id assigned) and becomes visible,
    but not committed. ``POST /api/assistant/ask`` commits; tests may roll back.
    """
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("question must not be empty")
    resolved = settings or get_settings()
    context = retrieve_for_question(
        session,
        cleaned,
        settings=resolved,
        embedder=embedder,
        min_score=min_score,
        top_k=top_k,
    )

    reply = _reply_for(cleaned, context, resolved, answerer)

    row = AssistantAnswer(
        question=cleaned,
        answer=reply.answer,
        citations=[citation.as_dict() for citation in reply.citations],
        model=reply.model,
        disclaimer_shown=True,
    )
    session.add(row)
    session.flush()
    logger.info(
        "assistant answer stored: id=%s citations=%d refused=%s",
        row.id,
        len(reply.citations),
        reply.refused,
    )
    return reply


#: Explicit alias for callers that read better as ``answer_question(...)``.
answer_question = ask
