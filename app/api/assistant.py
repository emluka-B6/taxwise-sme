"""Assistant endpoint: citation-backed answers over the versioned knowledge base.

``POST /api/assistant/ask`` returns ``{answer, citations, disclaimer}``. A question whose
best chunk is below ``ASSISTANT_MIN_SCORE`` is answered with the literal refusal
``insufficient sources`` (HTTP 200 - it is a valid, auditable outcome) and never reaches
the language model.

Operational guarantees enforced here:

* a process-local sliding-window rate limit per client host (429 + ``Retry-After``);
* logs carry only the question length, citation count and refusal flag - never the
  question or the answer text;
* provider configuration errors surface as 503, broken citation contracts as 502, so a
  misconfigured LLM can never produce a citation-less answer.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.assistant import answer as answer_service
from app.assistant.base import Answerer, Citation
from app.assistant.embedder import Embedder, build_embedder
from app.db.session import get_session
from app.settings import Settings, get_settings

__all__ = [
    "AssistantAskRequest",
    "AssistantAskResponse",
    "CitationRead",
    "RateLimiter",
    "get_answerer",
    "get_embedder",
    "get_rate_limiter",
    "router",
]

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

logger = logging.getLogger(__name__)

#: Clock injection point so the limiter can be tested without sleeping.
type Clock = Callable[[], float]

#: Maximum question length accepted by the endpoint.
MAX_QUESTION_CHARS: Final[int] = 2000


class RateLimiter:
    """Thread-safe sliding-window limiter keyed by an opaque client identifier.

    State is process-local: with multiple API workers each one enforces its own share.
    A shared store (Redis) would be the next step; the endpoint contract is unchanged.
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float = 60.0,
        clock: Clock = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")
        if window_seconds <= 0.0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    @property
    def limit(self) -> int:
        """Maximum number of events allowed inside the window."""
        return self._limit

    @property
    def window_seconds(self) -> float:
        """Width of the sliding window in seconds."""
        return self._window

    def check(self, key: str) -> float | None:
        """Record one event; return ``None`` or the ``Retry-After`` seconds when limited."""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self._window:
                events.popleft()
            if len(events) >= self._limit:
                return max(0.0, self._window - (now - events[0]))
            events.append(now)
            return None


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """Process-wide embedder selected by ``ASSISTANT_EMBEDDER`` (models load lazily)."""
    return build_embedder(get_settings())


@lru_cache(maxsize=1)
def get_answerer() -> Answerer:
    """Process-wide LLM provider selected by ``ASSISTANT_LLM`` (stub by default)."""
    try:
        return answer_service.build_answerer(get_settings())
    except answer_service.LLMNotConfigured as exc:
        # Dependency errors happen before the endpoint body; translate them here so the
        # documented 503 is what clients actually see.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    settings = get_settings()
    return RateLimiter(
        limit=settings.assistant_rate_limit_per_minute,
        window_seconds=settings.assistant_rate_limit_window_seconds,
    )


class AssistantAskRequest(BaseModel):
    """Payload of ``POST /api/assistant/ask``."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_QUESTION_CHARS,
            description="Tax question; answered only from allowlisted, versioned sources.",
        ),
    ]

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question must not be blank")
        return cleaned


class CitationRead(BaseModel):
    """A source actually used by the answer, including its version."""

    source_id: int
    url: str
    title: str
    published_at: datetime | None = None
    fetched_at: datetime
    version: str | None = None
    chunk_ordinal: int | None = None


class AssistantAskResponse(BaseModel):
    """Answer, the sources it cites and the mandatory disclaimer."""

    answer: str
    citations: list[CitationRead] = Field(default_factory=list)
    disclaimer: str


def _read_citation(citation: Citation) -> CitationRead:
    return CitationRead.model_validate(citation.as_dict())


def _enforce_rate_limit(
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Reject the request with 429 once the per-host window is exhausted."""
    client_key = request.client.host if request.client else "unknown"
    retry_after = limiter.check(client_key)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Assistant rate limit exceeded; try again later.",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )


@router.post(
    "/ask",
    response_model=AssistantAskResponse,
    summary="Ask a tax question",
    dependencies=[Depends(_enforce_rate_limit)],
    responses={
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Per-host rate limit exceeded"},
        status.HTTP_502_BAD_GATEWAY: {"description": "LLM provider or citation contract error"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "LLM provider not configured"},
    },
)
def ask(
    payload: AssistantAskRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    answerer: Annotated[Answerer, Depends(get_answerer)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> AssistantAskResponse:
    """Answer from retrieved sources, persist the audit row and return the citations."""
    try:
        reply = answer_service.ask(
            session,
            payload.question,
            settings=settings,
            answerer=answerer,
            embedder=embedder,
        )
        session.commit()
    except answer_service.LLMNotConfigured as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (answer_service.LLMError, answer_service.LLMContractError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Assistant provider failed to produce a citable answer.",
        ) from exc

    logger.info(
        "assistant ask served: question_chars=%d citations=%d refused=%s",
        len(payload.question),
        len(reply.citations),
        reply.refused,
    )
    return AssistantAskResponse(
        answer=reply.answer,
        citations=[_read_citation(citation) for citation in reply.citations],
        disclaimer=reply.disclaimer,
    )
