"""Cross-database column types.

SQLite runs locally and PostgreSQL runs in production, so anything that behaves
differently between the two dialects lives here. Models must never import a
dialect-specific type directly.

Deliberately *not* used: pgvector. Embeddings are plain TEXT holding a JSON array, which
keeps the schema portable; a later migration may swap the column for a native vector type.
"""

from __future__ import annotations

import json
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import JSON, Enum, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = ["EmbeddingVector", "JSONType", "sa_enum"]

#: ``JSON`` everywhere, ``JSONB`` on PostgreSQL.
JSONType: TypeEngine[Any] = JSON().with_variant(JSONB(), "postgresql")


class EmbeddingVector(TypeDecorator[list[float]]):
    """Embedding stored as JSON text (``[0.1, -0.2, ...]``) on every dialect."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps([float(item) for item in value], separators=(",", ":"))

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise TypeError("Embedding column must contain a JSON array")
        return [float(item) for item in decoded]


def _enum_values(enum_cls: type[PyEnum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


def sa_enum(enum_cls: type[PyEnum], *, name: str, length: int = 32) -> Enum:
    """Emulated (``VARCHAR`` + ``CHECK``) enum so DDL is identical on SQLite and PostgreSQL.

    Native PostgreSQL enum types would require extra ``CREATE TYPE``/``DROP TYPE``
    bookkeeping in every migration; the CHECK constraint keeps migrations boring.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
        length=length,
    )
