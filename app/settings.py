"""Application settings loaded from environment variables / ``.env``.

``DATABASE_URL`` is the only required knob in production; locally it falls back to a
file-based SQLite database. Tax rates and thresholds are *not* settings - they live in
the ``tax_year_params`` table so that a rate change never requires a code change.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_DATABASE_SCHEMES = frozenset({"sqlite", "postgresql"})

__all__ = ["ALLOWED_DATABASE_SCHEMES", "Settings", "get_settings"]


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "TaxWise SME API"
    app_version: str = "0.1.0"
    environment: Literal["local", "test", "staging", "prod"] = "local"
    debug: bool = False
    database_url: str = "sqlite+pysqlite:///./taxwise.db"
    sql_echo: bool = False

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        scheme = value.split(":", 1)[0].split("+", 1)[0]
        if scheme not in ALLOWED_DATABASE_SCHEMES:
            raise ValueError(
                f"DATABASE_URL must use one of {sorted(ALLOWED_DATABASE_SCHEMES)}, got {scheme!r}"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached; clear with ``get_settings.cache_clear()``)."""
    return Settings()
