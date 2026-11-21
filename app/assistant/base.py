"""Value objects and protocol for answer generation (no LLM SDK required).

A provider only turns a question plus retrieved chunks into text. Citations are *not*
trusted from the provider: :mod:`app.assistant.answer` re-derives them from the
``[source_id]`` markers in the text and the chunks that were actually retrieved, so a
model can never invent a URL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.assistant.retriever import RetrievedChunk

__all__ = ["Answerer", "AssistantReply", "Citation"]


@dataclass(frozen=True, slots=True)
class Citation:
    """Reference to one versioned knowledge source backing (part of) an answer."""

    title: str
    url: str
    chunk_ordinal: int | None = None
    source_id: int | None = None
    published_at: datetime | None = None
    fetched_at: datetime | None = None
    version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form persisted in ``assistant_answers.citations`` and returned by the API."""
        return {
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "version": self.version,
            "chunk_ordinal": self.chunk_ordinal,
        }


@dataclass(frozen=True, slots=True)
class AssistantReply:
    """Model output plus everything needed to persist an ``AssistantAnswer`` row."""

    answer: str
    citations: tuple[Citation, ...]
    model: str
    disclaimer: str
    refused: bool = False


@runtime_checkable
class Answerer(Protocol):
    """Anything that can answer a tax question from retrieved, versioned context.

    Implementations must answer only from ``context`` and must mark claims with a
    ``[source_id]`` citation. Provider citations are ignored: the service re-derives them
    from the markers and the actually retrieved chunks, so URLs cannot be invented.
    """

    @property
    def name(self) -> str:
        """Stable provider/model identifier stored on every answer row."""
        ...

    def answer(self, question: str, context: Sequence[RetrievedChunk]) -> AssistantReply:
        """Produce a grounded answer; return the refusal when ``context`` is empty."""
        ...
