"""Application settings loaded from environment variables / ``.env``.

``DATABASE_URL`` is the only required knob in production; locally it falls back to a
file-based SQLite database. Tax rates and thresholds are *not* settings - they live in
the ``tax_year_params`` table so that a rate change never requires a code change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_DATABASE_SCHEMES = frozenset({"sqlite", "postgresql"})

#: Default root of the content-addressed upload storage; ``DOCUMENTS_DIR`` overrides it.
DOCUMENTS_DIR: Final[Path] = Path("documents")

__all__ = ["ALLOWED_DATABASE_SCHEMES", "DOCUMENTS_DIR", "Settings", "get_settings"]


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
    documents_dir: Path = DOCUMENTS_DIR

    # --- assistant ---------------------------------------------------------
    #: Minimum cosine similarity a chunk must reach to be used as a source.
    assistant_min_score: float = 0.25
    #: Number of chunks handed to the answer provider (highest scores first).
    assistant_top_k: int = Field(default=5, ge=1, le=50)
    #: ``hashing`` is deterministic and dependency-free; ``sentence-transformers`` is the
    #: optional local extra. The value must match the embedder used during ingestion.
    assistant_embedder: str = "hashing"
    assistant_embedding_model: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    #: ``stub`` (default, deterministic) or ``openai`` (any OpenAI-compatible chat API).
    assistant_llm: str = "stub"
    assistant_llm_model: str = "gpt-4o-mini"
    #: Read from the environment only; never commit a real key. ``None`` disables the
    #: ``openai`` provider with a clear configuration error instead of a network call.
    assistant_llm_api_key: SecretStr | None = None
    assistant_llm_base_url: str = "https://api.openai.com/v1"
    assistant_llm_timeout_seconds: float = Field(default=30.0, gt=0.0)
    #: Sliding-window rate limit for ``POST /api/assistant/ask`` per client host.
    assistant_rate_limit_per_minute: int = Field(default=20, ge=1, le=10_000)
    assistant_rate_limit_window_seconds: float = Field(default=60.0, gt=0.0)

    # --- retention and audit ----------------------------------------------
    #: Documents whose ``created_at`` is older than this many years are retention
    #: candidates; 5 years matches the accounting-record retention rule in Poland.
    retention_years: int = Field(default=5, ge=1, le=50)
    #: Required to confirm an irreversible purge (``POST /api/admin/retention/run`` with
    #: ``dry_run=false``). Read from the environment only; ``None`` disables the purge.
    retention_confirmation_token: SecretStr | None = None

    # --- invoice API imports ----------------------------------------------
    #: Base URL of an accounting platform API. There is deliberately no default:
    #: the ``ApiProvider`` skeleton refuses to run until an operator configures it.
    accounting_api_url: str | None = None
    #: Read from the environment only; never commit a real token.
    accounting_api_token: SecretStr | None = None
    #: Path appended to ``ACCOUNTING_API_URL`` (e.g. ``invoices``).
    accounting_api_invoices_path: str = "invoices"
    accounting_api_timeout_seconds: float = Field(default=30.0, gt=0.0)

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        scheme = value.split(":", 1)[0].split("+", 1)[0]
        if scheme not in ALLOWED_DATABASE_SCHEMES:
            raise ValueError(
                f"DATABASE_URL must use one of {sorted(ALLOWED_DATABASE_SCHEMES)}, got {scheme!r}"
            )
        return value

    @field_validator("assistant_min_score")
    @classmethod
    def _check_min_score(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"ASSISTANT_MIN_SCORE must be within [0, 1], got {value}")
        return value

    @field_validator("assistant_llm", "assistant_embedder")
    @classmethod
    def _normalise_provider_names(cls, value: str) -> str:
        return value.strip().lower()



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached; clear with ``get_settings.cache_clear()``)."""
    return Settings()
