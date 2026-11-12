"""Shared fixtures.

Model tests run against a real SQLite file (not ``:memory:``) so that DDL, CHECK
constraints and indexes behave exactly as they will locally.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Env-var overrides must never leak between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'models.db'}")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(sqlite_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False)
    with factory() as db_session:
        yield db_session
        db_session.rollback()
