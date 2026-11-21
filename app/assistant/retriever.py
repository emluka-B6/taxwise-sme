"""Cosine top-k retrieval over the versioned knowledge base.

The knowledge base deliberately has no vector index (see ``app.db.types``), so retrieval
loads the chunk embeddings and scores them in Python. This keeps SQLite and PostgreSQL
behaviour identical and is fast enough for the size of an official-source corpus.

Two guardrails live here rather than in the prompt:

* only chunks whose ``KnowledgeSource.url`` passes
  :func:`app.assistant.ingest.validate_url` are candidates, so a row inserted by a buggy
  importer can never become a citation;
* a chunk is only returned when its cosine similarity to the question reaches
  ``ASSISTANT_MIN_SCORE`` - below that the answer layer refuses instead of guessing.

The module logs counts and source ids, never question text.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.assistant.base import Citation
from app.assistant.embedder import Embedder, build_embedder
from app.assistant.ingest import DomainNotAllowed, validate_url
from app.db.models import KnowledgeChunk
from app.domain.enums import KnowledgePublisher
from app.settings import Settings, get_settings

__all__ = [
    "DEFAULT_TOP_K",
    "RetrievedChunk",
    "cosine_similarity",
    "retrieve",
    "retrieve_for_question",
]

logger = logging.getLogger(__name__)

#: Number of chunks passed to the answer provider when ``ASSISTANT_TOP_K`` is unset.
DEFAULT_TOP_K: Final[int] = 5


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A chunk that passed the similarity threshold, with its versioned source metadata."""

    chunk_id: int
    source_id: int
    ordinal: int
    text: str
    score: float
    title: str
    url: str
    publisher: KnowledgePublisher
    published_at: datetime | None
    fetched_at: datetime
    version: str | None

    def to_citation(self) -> Citation:
        """Citation carrying the URL, publication date and the source version."""
        return Citation(
            title=self.title,
            url=self.url,
            chunk_ordinal=self.ordinal,
            source_id=self.source_id,
            published_at=self.published_at,
            fetched_at=self.fetched_at,
            version=self.version,
        )


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two equally sized vectors; zero vectors score ``0.0``."""
    if len(left) != len(right):
        raise ValueError(f"vector dimension mismatch: {len(left)} != {len(right)}")
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    score = dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
    # Guard against floating point drift outside the cosine range.
    return max(-1.0, min(1.0, score))


def retrieve(
    session: Session,
    question: str,
    *,
    embedder: Embedder,
    min_score: float | None = None,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Return the ``top_k`` chunks scoring at least ``min_score``, best first.

    ``min_score`` and ``top_k`` default to ``ASSISTANT_MIN_SCORE``/``ASSISTANT_TOP_K``.
    The threshold is inclusive. Chunks from sources outside the ingestion allowlist and
    chunks without an embedding are skipped. Ordering is deterministic: score descending,
    then source id and chunk ordinal ascending.
    """
    settings = get_settings()
    threshold = settings.assistant_min_score if min_score is None else min_score
    limit = settings.assistant_top_k if top_k is None else top_k
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"min_score must be within [0, 1], got {threshold}")
    if limit < 1:
        raise ValueError(f"top_k must be positive, got {limit}")
    cleaned = question.strip()
    if not cleaned:
        return []
    query_vectors = embedder.embed([cleaned])
    if not query_vectors:
        raise ValueError("embedder returned no vector for the question")
    query_vector = query_vectors[0]

    statement = (
        select(KnowledgeChunk)
        .options(joinedload(KnowledgeChunk.source))
        .where(KnowledgeChunk.embedding.is_not(None))
    )
    candidates = 0
    matched: list[RetrievedChunk] = []
    for chunk in session.scalars(statement):
        embedding = chunk.embedding
        if embedding is None:
            continue
        candidates += 1
        source = chunk.source
        try:
            url = validate_url(source.url)
        except DomainNotAllowed:
            logger.warning("knowledge source outside the allowlist skipped: id=%s", source.id)
            continue
        if len(embedding) != len(query_vector):
            logger.debug(
                "chunk dimension mismatch skipped: chunk_id=%s dimension=%d",
                chunk.id,
                len(embedding),
            )
            continue
        score = cosine_similarity(query_vector, embedding)
        if not math.isfinite(score) or score < threshold:
            continue
        matched.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                source_id=source.id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                score=score,
                title=source.title,
                url=url,
                publisher=source.publisher,
                published_at=source.published_at,
                fetched_at=source.fetched_at,
                version=source.version,
            )
        )

    matched.sort(key=lambda item: (-item.score, item.source_id, item.ordinal))
    selected = matched[:limit]
    logger.info(
        "knowledge retrieval: candidates=%d matched=%d selected=%d min_score=%.3f",
        candidates,
        len(matched),
        len(selected),
        threshold,
    )
    return selected


def retrieve_for_question(
    session: Session,
    question: str,
    *,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    min_score: float | None = None,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Retrieve using ``ASSISTANT_MIN_SCORE``/``ASSISTANT_TOP_K`` unless overridden.

    ``embedder`` must be the backend that produced the stored vectors; the API resolves it
    once per process from ``ASSISTANT_EMBEDDER``.
    """
    resolved = settings or get_settings()
    resolved_embedder = embedder or build_embedder(resolved)
    return retrieve(
        session,
        question,
        embedder=resolved_embedder,
        min_score=resolved.assistant_min_score if min_score is None else min_score,
        top_k=resolved.assistant_top_k if top_k is None else top_k,
    )
