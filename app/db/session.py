"""Engine/session factories.

The engine is created lazily so importing the app (e.g. in tests or in Alembic) never
opens a database connection or creates a file as a side effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.settings import get_settings

__all__ = ["create_db_engine", "get_engine", "get_session", "get_sessionmaker"]


def create_db_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Build an engine with dialect-appropriate options."""
    url = database_url or get_settings().database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # FastAPI serves requests from a thread pool; SQLite needs this flag.
        connect_args["check_same_thread"] = False
    return create_engine(url, echo=echo, pool_pre_ping=True, connect_args=connect_args)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine for the configured ``DATABASE_URL``."""
    settings = get_settings()
    return create_db_engine(settings.database_url, echo=settings.sql_echo)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Process-wide session factory bound to :func:`get_engine`."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session with commit/rollback handled by the caller."""
    with get_sessionmaker()() as session:
        yield session
