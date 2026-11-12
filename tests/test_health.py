"""Smoke tests for the HTTP API and settings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.settings import Settings


def test_health_returns_200_and_status_ok() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"]
    assert body["version"]


def test_health_is_documented_in_openapi() -> None:
    with TestClient(create_app()) as client:
        schema = client.get("/openapi.json").json()

    assert "/health" in schema["paths"]
    assert "get" in schema["paths"]["/health"]


def test_database_url_scheme_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://user@localhost/db")
    with pytest.raises(ValidationError):
        Settings()


def test_default_database_url_is_local_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings().database_url.startswith("sqlite+pysqlite:///")
