"""Declarative base shared by every ORM model.

The naming convention makes constraint names deterministic, which keeps Alembic
autogenerate diffs stable and lets migrations drop constraints by name on both
SQLite (batch mode) and PostgreSQL.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

__all__ = ["NAMING_CONVENTION", "Base"]


class Base(DeclarativeBase):
    """Base class for all mapped classes."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
