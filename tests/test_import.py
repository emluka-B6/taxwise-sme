"""Invoice import tests: CSV mappings, idempotency, row errors and API security.

Acceptance criteria covered here:

* a provider CSV fixture creates exactly as many ``documents`` as it has rows;
* re-importing the same file creates no duplicates (upsert by source + external id);
* one row with an invalid amount is reported in ``errors[]`` while the rest is
  imported;
* a changed row is updated and marked ``VERIFIED`` (manual verification);
* an unknown provider returns ``404``;
* ``ApiProvider`` without a URL/token raises a configuration error before it
  touches its HTTP client (zero requests).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.domain.enums import DocumentSource, DocumentStatus, DocumentType
from app.integrations.accounting import (
    ApiProvider,
    CsvMapping,
    ProviderConfigurationError,
    build_api_provider,
    load_mapping,
)
from app.settings import Settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "imports"
MAPPINGS = Path(__file__).resolve().parents[1] / "config" / "import_mappings"


def _upload(client: TestClient, provider: str, fixture: str):
    content = (FIXTURES / fixture).read_bytes()
    return client.post(
        f"/api/imports/{provider}",
        files={"file": (fixture, content, "text/csv")},
    )


def _documents(session: Session) -> list[Document]:
    return list(session.scalars(select(Document).order_by(Document.id)))


class _RecordingClient:
    """Mock HTTP client: records calls so a test can prove zero requests happened."""

    def __init__(self, payload: bytes = b"[]") -> None:
        self.payload = payload
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> bytes:
        self.calls.append((url, headers, timeout))
        return self.payload


# ----------------------------------------------------------------------- CSV import


def test_import_csv_fixture_creates_documents(api_client: TestClient, session: Session) -> None:
    response = _upload(api_client, "infakt", "infakt.csv")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"created": 2, "updated": 0, "skipped": 0, "errors": []}

    documents = _documents(session)
    assert len(documents) == 2
    first, second = documents
    assert first.source is DocumentSource.IMPORT_INFAKT
    assert first.external_id == "FV/2026/03/001"
    assert first.type is DocumentType.VAT_INVOICE_SALES
    assert first.issue_date == date(2026, 3, 4)
    assert first.period == "2026-03"
    assert first.amount_net == Decimal("1000.00")
    assert first.vat_amount == Decimal("230.00")
    assert first.amount_gross == Decimal("1230.00")
    assert first.counterparty_nip == "5260250995"
    assert first.status is DocumentStatus.NEW
    assert second.external_id == "FV/2026/03/002"
    assert second.type is DocumentType.VAT_INVOICE_COST
    assert second.counterparty_nip is None


@pytest.mark.parametrize(
    ("provider", "fixture", "external_id", "gross"),
    [
        ("fakturownia", "fakturownia.csv", "FV 12/2026", Decimal("1476.00")),
        ("wfirma", "wfirma.csv", "WF/2026/03/1", Decimal("984.00")),
    ],
)
def test_each_provider_mapping_imports_its_fixture(
    api_client: TestClient,
    session: Session,
    provider: str,
    fixture: str,
    external_id: str,
    gross: Decimal,
) -> None:
    response = _upload(api_client, provider, fixture)

    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    document = _documents(session)[0]
    assert document.external_id == external_id
    assert document.amount_gross == gross


def test_reimport_is_idempotent_by_external_id(
    api_client: TestClient, session: Session
) -> None:
    first = _upload(api_client, "infakt", "infakt.csv")
    assert first.status_code == 200, first.text
    identifiers = [document.id for document in _documents(session)]

    second = _upload(api_client, "infakt", "infakt.csv")

    assert second.status_code == 200, second.text
    assert second.json() == {"created": 0, "updated": 0, "skipped": 2, "errors": []}
    documents = _documents(session)
    assert len(documents) == 2
    assert [document.id for document in documents] == identifiers



def test_invalid_amount_is_reported_without_aborting_the_import(
    api_client: TestClient, session: Session
) -> None:
    response = _upload(api_client, "infakt", "infakt_invalid_amount.csv")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert body["skipped"] == 0
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["row"] == 3
    assert error["external_id"] == "FV/2026/03/102"
    assert "amount_gross" in error["message"]

    documents = _documents(session)
    assert len(documents) == 2
    assert {document.external_id for document in documents} == {
        "FV/2026/03/101",
        "FV/2026/03/103",
    }


def test_conflicting_data_is_updated_and_marked_for_manual_verification(
    api_client: TestClient, session: Session
) -> None:
    assert _upload(api_client, "infakt", "infakt.csv").status_code == 200

    response = _upload(api_client, "infakt", "infakt_conflict.csv")

    assert response.status_code == 200, response.text
    assert response.json() == {"created": 0, "updated": 1, "skipped": 0, "errors": []}
    documents = _documents(session)
    assert len(documents) == 2
    changed = next(doc for doc in documents if doc.external_id == "FV/2026/03/001")
    assert changed.amount_gross == Decimal("1300.00")
    assert changed.status is DocumentStatus.VERIFIED


def test_unknown_provider_returns_404(api_client: TestClient) -> None:
    response = _upload(api_client, "nieznany", "infakt.csv")

    assert response.status_code == 404
    assert "unknown" in response.json()["detail"]["message"].lower()


def test_mappings_are_versioned_and_match_their_provider_source() -> None:
    expected = {
        "infakt": DocumentSource.IMPORT_INFAKT,
        "fakturownia": DocumentSource.IMPORT_FAKTUROWNIA,
        "wfirma": DocumentSource.IMPORT_WFIRMA,
    }
    for provider, source in expected.items():
        mapping = load_mapping(MAPPINGS / f"{provider}.yaml")
        assert isinstance(mapping, CsvMapping)
        assert mapping.version >= 1
        assert mapping.provider == provider
        assert mapping.source is source


def test_mappings_declare_no_secrets() -> None:
    # Config must stay free of credentials; the API token is read from ENV only.
    forbidden = ("token", "secret", "password", "api_key", "apikey")
    for path in sorted(MAPPINGS.glob("*.yaml")):
        text = path.read_text(encoding="utf-8").casefold()
        for word in forbidden:
            assert word not in text, f"{path.name} contains {word!r}"


# ------------------------------------------------------------------------ ApiProvider


def test_api_provider_without_token_makes_no_network_call() -> None:
    client = _RecordingClient()
    provider = ApiProvider(
        source=DocumentSource.IMPORT_INFAKT,
        base_url="https://api.example.invalid/v1",
        token=None,
        http_client=client,
    )

    with pytest.raises(ProviderConfigurationError, match="ACCOUNTING_API_TOKEN"):
        provider.fetch_invoices(date(2026, 3, 1))

    assert client.calls == []


def test_api_provider_without_url_makes_no_network_call() -> None:
    client = _RecordingClient()
    provider = ApiProvider(
        source=DocumentSource.IMPORT_INFAKT,
        base_url=None,
        token="test-token",
        http_client=client,
    )

    with pytest.raises(ProviderConfigurationError, match="ACCOUNTING_API_URL"):
        provider.fetch_invoices(date(2026, 3, 1))

    assert client.calls == []


def test_api_provider_builder_fails_fast_without_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCOUNTING_API_URL", raising=False)
    monkeypatch.delenv("ACCOUNTING_API_TOKEN", raising=False)

    # Explicit ``None`` overrides a developer's local .env, keeping the test hermetic.
    settings = Settings(accounting_api_url=None, accounting_api_token=None)
    with pytest.raises(ProviderConfigurationError):
        build_api_provider(settings)


def test_api_provider_reads_url_and_token_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCOUNTING_API_URL", "https://api.example.invalid/v1")
    monkeypatch.setenv("ACCOUNTING_API_TOKEN", "test-token")
    payload = (
        b'[{"external_id":"API/1","issue_date":"2026-03-04","amount_net":100,'
        b'"vat_amount":23,"amount_gross":123,"document_type":"VAT_INVOICE_SALES",'
        b'"counterparty_nip":"5260250995","period":"2026-03","currency":"PLN"}]'
    )
    client = _RecordingClient(payload)

    provider = build_api_provider(Settings(), http_client=client)
    invoices = provider.fetch_invoices(date(2026, 3, 1))

    assert len(invoices) == 1
    assert invoices[0].external_id == "API/1"
    assert invoices[0].amount_gross == Decimal("123.00")
    assert len(client.calls) == 1
    url, headers, _ = client.calls[0]
    assert url == "https://api.example.invalid/v1/invoices?since=2026-03-01"
    assert headers["Authorization"] == "Bearer test-token"
