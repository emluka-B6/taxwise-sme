"""Migration tests.

Acceptance criteria covered here:

* ``alembic upgrade head`` builds every table on a clean SQLite database;
* the migrated schema matches the ORM metadata (``alembic check`` -> no drift);
* downgrade is reversible;
* the same migration renders PostgreSQL DDL (JSONB, no native ENUM types) offline,
  which is what CI runs against a real PostgreSQL service.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def test_upgrade_head_and_check_on_clean_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    config = _alembic_config()
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables

    # Manual verification turned into a regression test: migration == ORM metadata.
    command.check(config)

    command.downgrade(config, "base")
    engine = create_engine(url)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert remaining == {"alembic_version"}


def test_upgrade_head_renders_postgresql_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://taxwise:secret@localhost:5432/taxwise")
    get_settings.cache_clear()

    config = _alembic_config()
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.upgrade(config, "head", sql=True)
    sql = buffer.getvalue()

    assert "CREATE TABLE documents" in sql
    assert "JSONB" in sql
    assert "CREATE TYPE" not in sql
    assert "NUMERIC(14, 2)" in sql
    assert "ix_documents_sha256" in sql
