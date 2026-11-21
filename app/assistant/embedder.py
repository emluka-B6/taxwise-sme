"""Text embedding boundary for the knowledge base.

Ingestion only needs one operation - turn a batch of chunk texts into vectors - so the
rest of the application depends on the :class:`Embedder` protocol rather than on a model
library. Two implementations ship with the project:

* :class:`SentenceTransformerEmbedder` - the production, local implementation. Model
  weights are downloaded on first use and the library is an optional extra
  (``pip install -e ".[embeddings]"``); nothing here contacts an external embedding API.
* :class:`HashingEmbedder` - a deterministic, dependency-free fallback used by tests and
  offline environments. It is *not* a semantic model, but identical texts always produce
  identical vectors, which is what the ingestion tests need.

Vectors are plain ``list[float]`` because ``KnowledgeChunk.embedding`` stores them as JSON
text (``app.db.types.EmbeddingVector``) - the schema deliberately avoids pgvector.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.settings import Settings

__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_MODEL_NAME",
    "Embedder",
    "EmbedderUnavailable",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "build_embedder",
]

#: Width of the deterministic fallback vector; small enough to keep test fixtures fast.
DEFAULT_EMBEDDING_DIMENSION: Final[int] = 256

#: Multilingual sentence-transformers model with Polish coverage and 384 dimensions.
DEFAULT_MODEL_NAME: Final[str] = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

#: Tokens keep Polish diacritics, so "składka" and "skladka" stay distinct tokens.
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-ząćęłńóśźż]+")


class EmbedderUnavailable(RuntimeError):
    """The local embedding backend is not installed in this environment."""


@runtime_checkable
class Embedder(Protocol):
    """Anything that can embed a batch of texts with a stable vector width."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...


class HashingEmbedder:
    """Deterministic feature-hashing embedder for tests and offline use.

    Each token is hashed with BLAKE2b (stable across processes and Python versions, unlike
    ``hash()``) into one of ``dimension`` buckets with a sign derived from the digest; the
    vector is L2-normalised so cosine similarity is a plain dot product. This detects that
    chunks are embedded and persisted, but carries no semantic meaning.
    """

    def __init__(self, dimension: int = DEFAULT_EMBEDDING_DIMENSION) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return normalised hashing vectors; empty text yields the zero vector."""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        normalized = unicodedata.normalize("NFC", text).lower()
        for token in _TOKEN_PATTERN.findall(normalized):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class SentenceTransformerEmbedder:
    """Local ``sentence-transformers`` model, loaded lazily on the first ``embed`` call.

    The import is deferred so that importing the application (or running tests that inject
    :class:`HashingEmbedder`) never pulls in torch or downloads weights.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME) -> None:
        self.model_name = model_name
        self._model: Any = None

    @property
    def model(self) -> Any:
        """The underlying ``SentenceTransformer`` instance."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise EmbedderUnavailable(
                    "sentence-transformers is not installed; install the 'embeddings' "
                    "extra (pip install -e '.[embeddings]') or inject HashingEmbedder"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` with the local model; vectors are L2-normalised."""
        if not texts:
            return []
        encoded: Any = self.model.encode(
            list(texts), convert_to_numpy=True, normalize_embeddings=True
        )
        vectors: list[list[float]] = [[float(value) for value in row] for row in encoded]
        return vectors


def build_embedder(settings: Settings) -> Embedder:
    """Build the embedder selected by ``ASSISTANT_EMBEDDER``.

    ``hashing`` (the default) needs no optional dependency and makes tests deterministic;
    ``sentence-transformers`` is the production quality option but requires the
    ``embeddings`` extra. The retrieval side must use the same backend as ingestion,
    otherwise vectors live in different spaces.
    """
    backend = settings.assistant_embedder.strip().lower()
    if backend == "hashing":
        return HashingEmbedder()
    if backend in {"sentence-transformers", "sentence_transformers"}:
        return SentenceTransformerEmbedder(settings.assistant_embedding_model)
    raise ValueError(
        "ASSISTANT_EMBEDDER must be 'hashing' or 'sentence-transformers', "
        f"got {settings.assistant_embedder!r}"
    )
