"""JPK_PIT builder, export endpoints and tax pack tests.

Acceptance criteria covered here: the generated statement matches the golden file byte for
byte, rows carry the document date/amounts/counterparty NIP as ``Decimal`` values, an empty
period is a valid statement with zero totals, structural validation catches missing
elements, bad types and totals that disagree with the documents (``422`` from the API), XSD
validation is reported as ``SKIPPED`` when the optional JPK_PIT schema is not configured or
not downloaded, and the tax-pack ZIP contains a manifest whose SHA-256 checksums match the
archived files. File names never contain a NIP or a counterparty name.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import exports
from app.db.models import Document
from app.domain.enums import DocumentSource, DocumentStatus, DocumentType
from app.integrations import schemas
from app.integrations.jpk import jpk_pit_builder as builder
from app.integrations.jpk.jpk_pit_builder import (
    JpkPitTotalsMismatch,
    JpkPitValidationError,
)
from app.integrations.schemas import ValidationStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_FILE = FIXTURES / "jpk_pit_expected.xml"

#: Fixed DataWytworzenia so the golden comparison is deterministic.
CREATED_AT = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)

#: Both values are checksum-valid Polish NIPs used by the fixtures.
REVENUE_NIP = "9999999999"
COST_NIP = "5260250274"

STATEMENT_HEADER = "typ;data;nip_kontrahenta;kwota_netto;kwota_vat;kwota_brutto"


def make_document(**overrides: Any) -> Document:
    """A detached revenue document; the defaults match the first golden row."""
    values: dict[str, Any] = {
        "type": DocumentType.VAT_INVOICE_SALES,
        "issue_date": date(2026, 2, 10),
        "period": "2026-02",
        "amount_net": Decimal("1000.00"),
        "vat_amount": Decimal("230.00"),
        "amount_gross": Decimal("1230.00"),
        "currency": "PLN",
        "counterparty_nip": REVENUE_NIP,
        "source": DocumentSource.MANUAL,
        "status": DocumentStatus.VERIFIED,
    }
    values.update(overrides)
    return Document(**values)


def reference_documents() -> list[Document]:
    """One revenue and one cost document - the reference period used by the golden file."""
    return [
        make_document(),
        make_document(
            type=DocumentType.VAT_INVOICE_COST,
            issue_date=date(2026, 2, 12),
            amount_net=Decimal("400.00"),
            vat_amount=Decimal("92.00"),
            amount_gross=Decimal("492.00"),
            counterparty_nip=COST_NIP,
        ),
    ]


@pytest.fixture
def stored_documents(session: Session) -> list[Document]:
    """The reference documents persisted so the endpoints can select them."""
    rows = reference_documents()
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def _paths(xml: bytes, *, documents: list[Document] | None = None) -> set[str]:
    violations = builder.validate_jpk_pit(xml, documents=documents)
    return {violation.path for violation in violations}


# ---------------------------------------------------------------- builder (golden file)


def test_golden_file_is_byte_identical() -> None:
    """The reference period renders exactly like ``tests/fixtures/jpk_pit_expected.xml``."""
    xml = builder.build(reference_documents(), year=2026, month=2, created_at=CREATED_AT)
    assert xml == GOLDEN_FILE.read_bytes()


def test_document_states_the_file_was_not_submitted_to_mf() -> None:
    xml = builder.build(reference_documents(), year=2026, month=2, created_at=CREATED_AT)
    assert "NIE został przesłany do systemu MF" in xml.decode("utf-8")


def test_rows_are_ordered_and_carry_decimal_amounts() -> None:
    rows = builder.collect_rows(reference_documents())
    assert [row.typ for row in rows] == [builder.TYP_PRZYCHOD, builder.TYP_KOSZT]
    assert [row.data for row in rows] == [date(2026, 2, 10), date(2026, 2, 12)]
    assert all(isinstance(row.netto, Decimal) for row in rows)
    assert all(isinstance(row.brutto, Decimal) for row in rows)

    summary = builder.summarize(rows)
    assert summary.liczba_przychodow == 1
    assert summary.liczba_kosztow == 1
    assert summary.przychody_brutto == Decimal("1230.00")
    assert summary.koszty_brutto == Decimal("492.00")
    assert summary.saldo_brutto == Decimal("738.00")


def test_empty_period_is_a_valid_statement_with_zero_totals() -> None:
    """Documented choice: a month without documents still produces a valid zero file."""
    xml = builder.build([], year=2026, month=1, created_at=CREATED_AT)
    assert builder.validate_jpk_pit(xml) == []
    text = xml.decode("utf-8")
    assert "<DataOd>2026-01-01</DataOd>" in text
    assert "<DataDo>2026-01-31</DataDo>" in text
    assert "<Pozycje />" in text
    assert "<LiczbaPrzychodow>0</LiczbaPrzychodow>" in text
    assert "<SumaPrzychodowBrutto>0.00</SumaPrzychodowBrutto>" in text
    assert "<SaldoBrutto>0.00</SaldoBrutto>" in text


def test_document_without_a_net_vat_split_is_rejected() -> None:
    with pytest.raises(JpkPitValidationError) as info:
        builder.build(
            [make_document(amount_net=None, vat_amount=None)], year=2026, month=2
        )
    paths = {violation.path for violation in info.value.violations}
    assert "document/amount_net" in paths
    assert "document/vat_amount" in paths


def test_missing_or_invalid_counterparty_nip_is_rejected() -> None:
    with pytest.raises(JpkPitValidationError) as info:
        builder.build([make_document(counterparty_nip="123")], year=2026, month=2)
    assert "document/counterparty_nip" in {v.path for v in info.value.violations}


def test_document_totals_mismatch_is_reported() -> None:
    with pytest.raises(JpkPitTotalsMismatch) as info:
        builder.build(
            [make_document(amount_gross=Decimal("1300.00"))], year=2026, month=2
        )
    assert "net + VAT" in str(info.value)
    assert "document/amount_gross" in {v.path for v in info.value.violations}


def test_unsupported_document_type_is_rejected() -> None:
    with pytest.raises(JpkPitValidationError) as info:
        builder.build([make_document(type=DocumentType.ZUS)], year=2026, month=2)
    assert "document/type" in {v.path for v in info.value.violations}


# ---------------------------------------------------------------- structural validation


def test_structural_validation_accepts_the_golden_file() -> None:
    assert builder.validate_jpk_pit(GOLDEN_FILE.read_bytes()) == []


def test_structural_validation_flags_missing_header_element() -> None:
    broken = GOLDEN_FILE.read_bytes().replace(b"    <DataOd>2026-02-01</DataOd>\n", b"")
    assert "Naglowek/DataOd" in _paths(broken)


def test_structural_validation_flags_a_bad_amount_type() -> None:
    broken = GOLDEN_FILE.read_bytes().replace(
        b"<KwotaVat>230.00</KwotaVat>", b"<KwotaVat>230.001</KwotaVat>"
    )
    assert "Pozycje/Pozycja[1]/KwotaVat" in _paths(broken)


def test_structural_validation_flags_totals_that_disagree_with_documents() -> None:
    broken = GOLDEN_FILE.read_bytes().replace(
        b"<SumaPrzychodowBrutto>1230.00</SumaPrzychodowBrutto>",
        b"<SumaPrzychodowBrutto>9999.00</SumaPrzychodowBrutto>",
    )
    paths = _paths(broken, documents=reference_documents())
    assert "Podsumowanie/SumaPrzychodowBrutto" in paths


def test_structural_validation_reports_malformed_and_foreign_documents() -> None:
    assert builder.validate_jpk_pit(b"<JPK_PIT>")[0].path == "document"
    foreign = builder.validate_jpk_pit(b"<Faktura/>")
    assert foreign and foreign[0].path == "JPK_PIT"


# ---------------------------------------------------------------- API


def test_endpoint_returns_xml_marked_as_not_submitted(
    api_client: TestClient, stored_documents: list[Document]
) -> None:
    response = api_client.get("/api/exports/jpk-pit", params={"year": 2026, "month": 2})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/xml"
    assert response.headers["X-JPK-PIT-Transmission"] == "not-sent"
    assert response.headers["X-Schema-Validation"] == "skipped"
    assert response.headers["X-JPK-PIT-Schema"] == "unconfirmed"
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="jpk_pit_2026_02.xml"'
    )
    assert builder.validate_jpk_pit(response.content, documents=stored_documents) == []
    assert b"<SumaPrzychodowBrutto>1230.00</SumaPrzychodowBrutto>" in response.content


def test_endpoint_renders_an_empty_month_as_a_valid_zero_file(
    api_client: TestClient,
) -> None:
    response = api_client.get("/api/exports/jpk-pit", params={"year": 2026, "month": 1})
    assert response.status_code == 200, response.text
    assert builder.validate_jpk_pit(response.content) == []
    assert b"<LiczbaPrzychodow>0</LiczbaPrzychodow>" in response.content


def test_endpoint_reports_inconsistent_documents_as_422(
    api_client: TestClient, session: Session
) -> None:
    row = make_document(amount_gross=Decimal("1300.00"))
    session.add(row)
    session.commit()
    session.refresh(row)

    response = api_client.get("/api/exports/jpk-pit", params={"year": 2026, "month": 2})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["submitted_to_mf"] is False
    assert any(
        violation["path"].endswith("amount_gross") for violation in detail["violations"]
    )
    assert response.headers["X-Schema-Validation"] == "skipped"


def test_endpoint_rejects_an_invalid_month(api_client: TestClient) -> None:
    response = api_client.get("/api/exports/jpk-pit", params={"year": 2026, "month": 13})
    assert response.status_code == 422


# ---------------------------------------------------------------- XSD status and tax pack


def test_xsd_validation_is_skipped_without_a_downloaded_schema(tmp_path: Path) -> None:
    result = schemas.check(GOLDEN_FILE.read_bytes(), tmp_path / "JPK_PIT.xsd")
    assert result.status is ValidationStatus.SKIPPED
    assert result.passed is True


def test_endpoint_marks_validation_skipped_when_jpk_pit_is_not_configured(
    api_client: TestClient,
    stored_documents: list[Document],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry without a ``jpk_pit`` entry is not an error: the response says skipped."""

    def missing(*args: object, **kwargs: object) -> schemas.SchemaSpec:
        raise schemas.SchemaConfigError("no schema named 'jpk_pit' in the registry")

    monkeypatch.setattr(exports.schemas, "get", missing)
    response = api_client.get("/api/exports/jpk-pit", params={"year": 2026, "month": 2})
    assert response.status_code == 200, response.text
    assert response.headers["X-Schema-Validation"] == "skipped"


def test_tax_pack_contains_a_manifest_with_checksums(
    api_client: TestClient, stored_documents: list[Document]
) -> None:
    response = api_client.get("/api/exports/tax-pack", params={"year": 2026})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert (
        response.headers["Content-Disposition"]
        == 'attachment; filename="tax_pack_2026.zip"'
    )

    expected = {"jpk_pit_2026.xml", "statement_2026.csv", "manifest.json"}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert names == expected
        assert all(REVENUE_NIP not in name and COST_NIP not in name for name in names)

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["year"] == 2026
        assert manifest["submitted_to_mf"] is False
        assert manifest["jpk_pit_schema_status"] == "skipped"
        entries = {entry["name"]: entry for entry in manifest["files"]}
        assert set(entries) == expected - {"manifest.json"}
        for name, entry in entries.items():
            data = archive.read(name)
            assert entry["sha256"] == hashlib.sha256(data).hexdigest()
            assert entry["bytes"] == len(data)

        xml = archive.read("jpk_pit_2026.xml")
        assert b"<DataOd>2026-01-01</DataOd>" in xml
        assert b"<DataDo>2026-12-31</DataDo>" in xml
        assert builder.validate_jpk_pit(xml) == []

        statement = archive.read("statement_2026.csv").decode("utf-8").splitlines()
        assert statement[0] == STATEMENT_HEADER
        assert "PRZYCHOD;2026-02-10;9999999999;1000.00;230.00;1230.00" in statement
        assert "KOSZT;2026-02-12;5260250274;400.00;92.00;492.00" in statement
