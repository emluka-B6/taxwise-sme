"""Alembic environment.

The URL comes from ``DATABASE_URL`` (via ``app.settings``) so ``alembic upgrade head``
works against a clean SQLite file locally and a clean PostgreSQL database in CI without
editing a single file.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `alembic` invocations from other cwds
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import models  # noqa: E402,F401  - imported for side effect: register tables
from app.db.base import Base  # noqa: E402
from app.settings import get_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False keeps pytest's logging alive during test-run migrations.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
# ConfigParser treats % specially; escape it (passwords in URLs often contain %).
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without a DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_is_sqlite(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER most things; batch mode recreates the table instead.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
