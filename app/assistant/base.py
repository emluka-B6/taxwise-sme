"""Value objects and protocol for answer generation (no LLM SDK wired in yet)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["Answerer", "AssistantReply", "Citation"]


@dataclass(frozen=True, slots=True)
class Citation:
    """Reference to a knowledge source backing (part of) an answer."""

    title: str
    url: str
    chunk_ordinal: int | None = None


@dataclass(frozen=True, slots=True)
class AssistantReply:
    """Model output plus everything needed to persist an ``AssistantAnswer`` row."""

    answer: str
    citations: tuple[Citation, ...]
    model: str
    disclaimer: str


@runtime_checkable
class Answerer(Protocol):
    """Anything that can answer a tax question from retrieved context."""

    def answer(self, question: str, context: Sequence[str]) -> AssistantReply:
        """Produce a grounded answer; implementations must not invent citations."""
        ...
