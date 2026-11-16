"""FA(3) builder and KSeF export endpoint tests.

Acceptance criteria covered here: the generated document matches the golden file byte for
byte, the FA(3) metadata comes from ``config/schemas.yaml``, totals are compared with the
stored document (mismatch -> 422), missing fields -> 422, the response is
``application/xml`` marked ``X-KSeF-Schema-Version: FA(3)`` / ``X-Schema-Validation``, and
validation is reported as ``skipped`` when no XSD has been downloaded.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import ksef_export
from app.db.models import Document
from app.domain.enums import DocumentSource, DocumentStatus, DocumentType
from app.integrations import schemas
from app.integrations.ksef import fa3_validate
from app.integrations.ksef.fa3_builder import (
    Fa3Invoice,
    Fa3TotalsMismatch,
    Fa3ValidationError,
    LineItem,
    Party,
    build,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_FILE = FIXTURES / "fa3_expected.xml"
MINIMAL_XSD = FIXTURES / "schemas" / "minimal.xsd"

#: Fixed DataWytworzeniaFa so the golden comparison is deterministic.
CREATED_AT = datetime(2026, 2, 15, 9, 30, tzinfo=UTC)

EXEMPTION_BASIS = "art. 43 ust. 1 pkt 3 ustawy o VAT"

SELLER = Party(
    nip="5260250274",
    name="TaxWise Jan Kowalski",
    address_line_1="ul. Kwiatowa 1",
    address_line_2="00-001 Warszawa",
)
BUYER = Party(
    nip="9999999999",
    name="ABC AGD sp. z o.o.",
    address_line_1="ul. Polna 1",
    address_line_2="00-001 Warszawa",
)
LINES = (
    LineItem("Usluga doradcza", Decimal("2"), "szt.", Decimal("1000.00"), "23"),
    LineItem("Szkolenie", Decimal("1"), "usl.", Decimal("500.00"), "8"),
    LineItem("Swiadczenie zwolnione", Decimal("1"), "usl.", Decimal("250.00"), "zw"),
)


def make_invoice(**overrides: Any) -> Fa3Invoice:
    """The reference invoice used by the golden file and the API tests."""
    values: dict[str, Any] = {
        "number": "FV/2026/02/1",
        "seller": SELLER,
        "buyer": BUYER,
        "lines": LINES,
        "sale_date": date(2026, 2, 10),
        "place": "Warszawa",
        "exemption_basis": EXEMPTION_BASIS,
    }
    values.update(overrides)
    return Fa3Invoice(**values)


def make_document(**overrides: Any) -> Document:
    """A detached VAT_INVOICE_SALES row; the amounts match :func:`make_invoice`."""
    values: dict[str, Any] = {
        "type": DocumentType.VAT_INVOICE_SALES,
        "issue_date": date(2026, 2, 15),
        "period": "2026-02",
        "amount_net": Decimal("2750.00"),
        "vat_amount": Decimal("500.00"),
        "amount_gross": Decimal("3250.00"),
        "currency": "PLN",
        "counterparty_nip": "9999999999",
        "source": DocumentSource.MANUAL,
        "status": DocumentStatus.VERIFIED,
    }
    values.update(overrides)
    return Document(**values)


def payload(**overrides: Any) -> dict[str, Any]:
    """Request body for the export endpoint, matching the golden invoice."""
    body: dict[str, Any] = {
        "number": "FV/2026/02/1",
        "seller": {
            "nip": "5260250274",
            "name": "TaxWise Jan Kowalski",
            "address_line_1": "ul. Kwiatowa 1",
            "address_line_2": "00-001 Warszawa",
        },
        "buyer": {
            "nip": "9999999999",
            "name": "ABC AGD sp. z o.o.",
            "address_line_1": "ul. Polna 1",
            "address_line_2": "00-001 Warszawa",
        },
        "lines": [
            {
                "description": "Usluga doradcza",
                "quantity": "2",
                "unit": "szt.",
                "unit_net_price": "1000.00",
                "vat_rate": "23",
            },
            {
                "description": "Szkolenie",
                "quantity": "1",
                "unit": "usl.",
                "unit_net_price": "500.00",
                "vat_rate": "8",
            },
            {
                "description": "Swiadczenie zwolnione",
                "quantity": "1",
                "unit": "usl.",
                "unit_net_price": "250.00",
                "vat_rate": "zw",
            },
        ],
        "sale_date": "2026-02-10",
        "place": "Warszawa",
        "exemption_basis": EXEMPTION_BASIS,
        "created_at": "2026-02-15T09:30:00Z",
    }
    body.update(overrides)
    return body


@pytest.fixture
def stored_document(session: Session) -> Document:
    """The reference document, persisted so the endpoint can load it by id."""
    row = make_document()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture
def client(api_client: TestClient) -> TestClient:
    return api_client


def export_xml(
    client: TestClient,
    document_id: int,
    body: dict[str, Any],
    *,
    validate: bool = False,
) -> httpx.Response:
    """``GET`` with a JSON body (``TestClient.get`` does not expose ``json=``)."""
    query = "?validate=true" if validate else ""
    return client.request(
        "GET", f"/api/documents/{document_id}/ksef-xml{query}", json=body
    )


# ---------------------------------------------------------------- builder (golden file)


def test_golden_file_is_byte_identical() -> None:
    """The reference invoice renders exactly like ``tests/fixtures/fa3_expected.xml``."""
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    assert xml == GOLDEN_FILE.read_bytes()


def test_document_states_the_file_was_not_sent_to_ksef() -> None:
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    assert "NIE został przesłany do KSeF" in xml.decode("utf-8")


def test_fa3_metadata_comes_from_the_schema_registry(tmp_path: Path) -> None:
    """Namespace, KodFormularza attributes, wariant and version are config, not literals."""
    base = schemas.get(schemas.FA3_KEY)
    config = tmp_path / "schemas.yaml"
    config.write_text(
        "\n".join(
            [
                "ksef_fa3:",
                '  version: "FA(3)-test"',
                '  namespace: "urn:taxwise:test:fa3"',
                '  kod_formularza: "FA"',
                '  kod_systemowy: "FA (3) TEST"',
                "  wariant_formularza: 3",
                '  wersja_schemy: "1-0E"',
                f'  path: "{base.path.as_posix()}"',
                '  url: "https://example.invalid/schemat.xsd"',
                f'  sha256: "{base.sha256}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    schemas.clear_cache()
    try:
        xml = build(
            make_document(), make_invoice(), created_at=CREATED_AT, config_path=config
        ).decode("utf-8")
    finally:
        schemas.clear_cache()

    assert 'xmlns="urn:taxwise:test:fa3"' in xml
    assert 'kodSystemowy="FA (3) TEST"' in xml
    assert "<WariantFormularza>3</WariantFormularza>" in xml


def test_totals_are_aggregated_per_rate_group() -> None:
    totals = make_invoice().totals()
    assert totals.net == Decimal("2750.00")
    assert totals.vat == Decimal("500.00")
    assert totals.gross == Decimal("3250.00")
    assert [(item.rate.value, item.net, item.vat) for item in totals.by_rate] == [
        ("23", Decimal("2000.00"), Decimal("460.00")),
        ("8", Decimal("500.00"), Decimal("40.00")),
        ("zw", Decimal("250.00"), Decimal("0.00")),
    ]


def test_line_vat_uses_the_rounded_group_net() -> None:
    """Ten 0.05 lines at 23% tax the group net (0.50 -> 0.12), not each line separately."""
    invoice = make_invoice(
        lines=tuple(
            LineItem("Pozycja", Decimal("1"), "szt.", Decimal("0.05"), "23") for _ in range(10)
        )
    )
    totals = invoice.totals()
    assert totals.net == Decimal("0.50")
    assert totals.vat == Decimal("0.12")


def test_vat_rate_accepts_a_decimal_fraction() -> None:
    line = LineItem("Pozycja", Decimal("1"), "szt.", Decimal("100.00"), Decimal("0.23"))
    assert line.rate.value == "23"


# ---------------------------------------------------------------- builder (validation)


def test_totals_mismatch_is_reported_with_violations() -> None:
    with pytest.raises(Fa3TotalsMismatch) as excinfo:
        build(
            make_document(amount_gross=Decimal("3000.00")),
            make_invoice(),
            created_at=CREATED_AT,
        )
    paths = {violation.path for violation in excinfo.value.violations}
    assert "document/amount_gross" in paths
    assert all(violation.message for violation in excinfo.value.violations)


def test_missing_values_are_collected_into_one_error() -> None:
    invoice = make_invoice(
        number="",
        seller=Party(nip="123", name="", address_line_1=""),
        buyer=Party(nip="", name="", address_line_1=""),
        lines=(),
    )
    with pytest.raises(Fa3ValidationError) as excinfo:
        build(make_document(), invoice, created_at=CREATED_AT)
    paths = {violation.path for violation in excinfo.value.violations}
    assert {
        "Fa/P_2",
        "Podmiot1/DaneIdentyfikacyjne/NIP",
        "Podmiot1/DaneIdentyfikacyjne/Nazwa",
        "Podmiot2/DaneIdentyfikacyjne/NIP",
        "Fa/FaWiersz",
    } <= paths


def test_exempt_lines_require_a_legal_basis() -> None:
    with pytest.raises(Fa3ValidationError) as excinfo:
        build(
            make_document(),
            make_invoice(exemption_basis=None),
            created_at=CREATED_AT,
        )
    assert [violation.path for violation in excinfo.value.violations] == [
        "Fa/Adnotacje/Zwolnienie/P_19"
    ]


def test_non_sales_documents_are_rejected() -> None:
    with pytest.raises(Fa3ValidationError) as excinfo:
        build(
            make_document(type=DocumentType.VAT_INVOICE_COST),
            make_invoice(),
            created_at=CREATED_AT,
        )
    assert excinfo.value.violations[0].path == "document/type"


def test_negative_line_price_is_rejected() -> None:
    invoice = make_invoice(
        lines=(LineItem("Pozycja", Decimal("1"), "szt.", Decimal("-1.00"), "23"),)
    )
    with pytest.raises(Fa3ValidationError) as excinfo:
        build(make_document(amount_net=None), invoice, created_at=CREATED_AT)
    paths = {violation.path for violation in excinfo.value.violations}
    assert "Fa/FaWiersz[1]/P_9A" in paths
    assert "document/amount_net" in paths


def test_currency_must_match_the_document() -> None:
    with pytest.raises(Fa3ValidationError) as excinfo:
        build(
            make_document(),
            make_invoice(currency="EUR"),
            created_at=CREATED_AT,
        )
    assert [violation.path for violation in excinfo.value.violations] == ["Fa/KodWaluty"]



# ---------------------------------------------------------------- structural validation


def test_generated_xml_passes_structural_validation() -> None:
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    assert fa3_validate.validate_fa3(xml) == []


def test_structural_validation_flags_an_inconsistent_total() -> None:
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    broken = xml.replace(b"<P_15>3250.00</P_15>", b"<P_15>9999.00</P_15>")
    violations = fa3_validate.validate_fa3(broken)
    assert [violation.path for violation in violations] == ["Fa/P_15"]
    assert "9999.00" in violations[0].message


def test_structural_validation_flags_a_rate_group_that_does_not_match_the_lines() -> None:
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    broken = xml.replace(b"<P_13_2>500.00</P_13_2>", b"<P_13_2>400.00</P_13_2>")
    paths = {violation.path for violation in fa3_validate.validate_fa3(broken)}
    assert "Fa/P_13_2" in paths
    assert "Fa/P_15" in paths


def test_structural_validation_flags_missing_required_elements() -> None:
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    broken = xml.replace(b"<P_2>FV/2026/02/1</P_2>", b"")
    paths = {violation.path for violation in fa3_validate.validate_fa3(broken)}
    assert "Fa/P_2" in paths


def test_structural_validation_reports_malformed_and_foreign_documents() -> None:
    assert fa3_validate.validate_fa3(b"<Faktura>")[0].path == "document"
    foreign = fa3_validate.validate_fa3(b"<invoice/>")
    assert foreign and foreign[0].path == "Faktura"


def test_structural_validation_rejects_a_wrong_namespace() -> None:
    xml = build(make_document(), make_invoice(), created_at=CREATED_AT)
    broken = xml.replace(
        b'xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/"', b'xmlns="urn:other"'
    )
    violations = fa3_validate.validate_fa3(broken)
    assert violations and violations[0].path == "Faktura"



# ---------------------------------------------------------------- API


def test_endpoint_returns_xml_marked_as_not_sent(
    client: TestClient, stored_document: Document
) -> None:
    response = export_xml(client, stored_document.id, payload())
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/xml"
    assert response.headers["X-KSeF-Schema-Version"] == schemas.fa3_format().version == "FA(3)"
    assert response.headers["X-Schema-Validation"] == "skipped"
    assert response.headers["X-KSeF-Transmission"] == "not-sent"
    assert response.headers["Content-Disposition"] == 'attachment; filename="FV_2026_02_1.xml"'
    assert response.content == GOLDEN_FILE.read_bytes()


def test_endpoint_returns_404_for_unknown_document(client: TestClient) -> None:
    response = export_xml(client, 9999, payload())
    assert response.status_code == 404


def test_endpoint_rejects_non_sales_documents(client: TestClient, session: Session) -> None:
    row = make_document(type=DocumentType.ZUS, counterparty_nip=None)
    session.add(row)
    session.commit()
    session.refresh(row)
    response = export_xml(client, row.id, payload())
    assert response.status_code == 409


def test_endpoint_reports_inconsistent_totals_as_422(
    client: TestClient, stored_document: Document
) -> None:
    body = payload(
        lines=[
            {
                "description": "Usluga doradcza",
                "quantity": "1",
                "unit": "szt.",
                "unit_net_price": "1000.00",
                "vat_rate": "23",
            }
        ]
    )
    response = export_xml(client, stored_document.id, body)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["sent_to_ksef"] is False
    assert {item["path"] for item in detail["violations"]} >= {
        "document/amount_net",
        "document/vat_amount",
        "document/amount_gross",
    }


def test_endpoint_reports_missing_fields_as_422(
    client: TestClient, stored_document: Document
) -> None:
    body = payload()
    del body["seller"]
    response = export_xml(client, stored_document.id, body)
    assert response.status_code == 422
    assert response.json()["detail"]


def test_endpoint_reports_invalid_values_as_422(
    client: TestClient, stored_document: Document
) -> None:
    body = payload()
    body["lines"][0]["vat_rate"] = "7"
    response = export_xml(client, stored_document.id, body)
    assert response.status_code == 422
    assert "unsupported VAT rate" in response.json()["detail"]["message"]



def test_endpoint_validates_against_an_available_schema(
    client: TestClient, stored_document: Document, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(schemas.get(schemas.FA3_KEY), path=MINIMAL_XSD)
    monkeypatch.setattr(ksef_export, "_schema_spec", lambda: spec)
    response = export_xml(client, stored_document.id, payload(), validate=True)
    assert response.status_code == 200, response.text
    assert response.headers["X-Schema-Validation"] == "ok"


def test_endpoint_reports_failed_xsd_validation_as_422(
    client: TestClient, stored_document: Document, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema is available and the document does not conform -> 422 + violation list."""
    failed = schemas.check(b"<invoice/>", MINIMAL_XSD)
    monkeypatch.setattr(ksef_export.schemas, "check", lambda *args, **kwargs: failed)
    response = export_xml(client, stored_document.id, payload(), validate=True)
    assert response.status_code == 422, response.text
    assert response.headers["X-Schema-Validation"] == "failed"
    assert response.headers["X-KSeF-Schema-Version"] == "FA(3)"
    detail = response.json()["detail"]
    assert detail["violations"] and detail["sent_to_ksef"] is False


def test_endpoint_skips_validation_when_the_schema_is_absent(
    client: TestClient, stored_document: Document, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(schemas.get(schemas.FA3_KEY), path=Path("resources/ksef/missing.xsd"))
    monkeypatch.setattr(ksef_export, "_schema_spec", lambda: spec)
    response = export_xml(client, stored_document.id, payload(), validate=True)
    assert response.status_code == 200, response.text
    assert response.headers["X-Schema-Validation"] == "skipped"

