"""Schema tests: cross-dialect types, money precision, constraints and defaults."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, Numeric, inspect, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.db.models import (
    AssistantAnswer,
    Decision,
    Document,
    KnowledgeChunk,
    KnowledgeSource,
    TaxProfile,
    TaxYearParams,
)
from app.domain.enums import (
    DocumentSource,
    DocumentStatus,
    DocumentType,
    KnowledgePublisher,
    PitRegime,
    VatStatus,
    ZusVariant,
)


def _document(**overrides: object) -> Document:
    payload: dict[str, object] = {
        "type": DocumentType.VAT_INVOICE_COST,
        "issue_date": date(2026, 3, 14),
        "period": "2026-03",
        "amount_net": Decimal("1000.00"),
        "vat_amount": Decimal("230.00"),
        "amount_gross": Decimal("1230.00"),
        "counterparty_nip": "5260250995",
        "source": DocumentSource.IMPORT_INFAKT,
        "external_id": "infa-1",
        "file_path": "uploads/2026/03/infa-1.pdf",
        "sha256": "a" * 64,
    }
    payload.update(overrides)
    return Document(**payload)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- types


def test_json_type_compiles_to_jsonb_on_postgresql() -> None:
    """JSONType must resolve to JSONB in PostgreSQL DDL (the production dialect)."""
    ddl = str(CreateTable(Decision.__table__).compile(dialect=postgresql.dialect()))
    assert "JSONB" in ddl
    assert "source_refs JSONB" in ddl


def test_json_type_compiles_to_json_on_sqlite() -> None:
    ddl = str(CreateTable(Decision.__table__).compile(dialect=sqlite.dialect()))
    assert "source_refs JSON" in ddl
    assert "JSONB" not in ddl


@pytest.mark.parametrize(
    "dialect", [sqlite.dialect(), postgresql.dialect()], ids=["sqlite", "postgresql"]
)
def test_every_table_compiles_on_both_dialects(dialect: object) -> None:
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))  # type: ignore[arg-type]
        assert f"CREATE TABLE {table.name}" in ddl


def test_postgresql_ddl_has_no_native_enum_types() -> None:
    dialect = postgresql.dialect()
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables
    )
    assert "CREATE TYPE" not in ddl
    assert "VARCHAR(32)" in ddl


def test_money_columns_are_numeric_14_2() -> None:
    money_columns = {
        "documents": ("amount_net", "vat_amount", "amount_gross"),
        "tax_profiles": ("vat_exemption_threshold_pln",),
    }
    for table_name, columns in money_columns.items():
        for column_name in columns:
            column_type = Base.metadata.tables[table_name].c[column_name].type
            assert isinstance(column_type, Numeric)
            assert (column_type.precision, column_type.scale) == (14, 2), column_name


# ---------------------------------------------------------------------- persistence


def test_document_roundtrip_and_defaults(session: Session) -> None:
    document = _document()
    session.add(document)
    session.commit()
    session.refresh(document)

    assert document.id is not None
    assert document.status is DocumentStatus.NEW
    assert document.currency == "PLN"
    assert document.created_at is not None
    stored = session.scalars(select(Document)).one()
    assert stored.amount_gross == Decimal("1230.00")
    assert stored.source is DocumentSource.IMPORT_INFAKT


def test_document_source_external_id_is_unique(session: Session) -> None:
    session.add(_document())
    session.commit()

    session.add(_document(sha256="b" * 64, file_path="uploads/other.pdf"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_same_external_id_allowed_for_different_sources(session: Session) -> None:
    session.add(_document(source=DocumentSource.IMPORT_INFAKT, external_id="shared-1"))
    session.add(
        _document(
            source=DocumentSource.IMPORT_WFIRMA,
            external_id="shared-1",
            sha256="c" * 64,
        )
    )
    session.commit()
    assert len(session.scalars(select(Document)).all()) == 2


def test_sha256_index_exists(sqlite_engine: Engine) -> None:
    index_names = {index["name"] for index in inspect(sqlite_engine).get_indexes("documents")}
    assert "ix_documents_sha256" in index_names


def test_json_payload_roundtrips_on_sqlite(session: Session) -> None:
    params = TaxYearParams(
        year=2026,
        version=1,
        payload={"vat": {"registered_rate": "from source"}, "notes": ["a", "b"]},
        source_url="https://example.gov.pl/params",
        effective_from=date(2026, 1, 1),
    )
    decision = Decision(
        decided_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        topic="vat_registration",
        choice="stay_exempt",
        source_refs=["https://example.gov.pl/params"],
    )
    session.add_all([params, decision])
    session.commit()

    stored_params = session.scalars(select(TaxYearParams)).one()
    stored_decision = session.scalars(select(Decision)).one()
    assert stored_params.payload == {
        "vat": {"registered_rate": "from source"},
        "notes": ["a", "b"],
    }
    assert stored_decision.source_refs == ["https://example.gov.pl/params"]


def test_tax_year_params_year_version_is_unique(session: Session) -> None:
    common = {"year": 2026, "version": 1, "payload": {}, "effective_from": date(2026, 1, 1)}
    session.add_all([TaxYearParams(**common), TaxYearParams(**common)])  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_embedding_is_json_text_and_roundtrips(session: Session) -> None:
    source = KnowledgeSource(
        publisher=KnowledgePublisher.MF,
        url="https://example.gov.pl/interpretation",
        title="Interpretacja ogolna",
        sha256="d" * 64,
        version="2026-01",
    )
    source.chunks.append(
        KnowledgeChunk(ordinal=0, text="Fragment tekstu.", embedding=[0.5, -0.25, 1.0])
    )
    session.add(source)
    session.commit()

    chunk = session.scalars(select(KnowledgeChunk)).one()
    assert chunk.embedding == [0.5, -0.25, 1.0]
    assert chunk.source.publisher is KnowledgePublisher.MF

    # The column really is TEXT holding a JSON array (no pgvector, no binary blob).
    raw = session.connection().exec_driver_sql("SELECT embedding FROM knowledge_chunks").scalar()
    assert isinstance(raw, str)
    assert raw == "[0.5,-0.25,1.0]"


def test_tax_profile_carries_no_baked_in_rates(session: Session) -> None:
    columns = TaxProfile.__table__.c
    assert columns.ip_box_rate.server_default is None
    assert columns.vat_exemption_threshold_pln.server_default is None

    profile = TaxProfile(
        pit_regime=PitRegime.SCALE,
        vat_status=VatStatus.REGISTERED,
        zus_variant=ZusVariant.FULL,
        valid_from=date(2026, 1, 1),
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    assert profile.ip_box_rate is None
    assert profile.vat_exemption_threshold_pln is None


def test_tax_profile_validity_range_is_enforced(session: Session) -> None:
    session.add(
        TaxProfile(
            pit_regime=PitRegime.LINEAR,
            vat_status=VatStatus.REGISTERED,
            zus_variant=ZusVariant.PREFERENTIAL_24M,
            valid_from=date(2026, 6, 1),
            valid_to=date(2026, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_assistant_answer_defaults_to_disclaimer_shown(session: Session) -> None:
    answer = AssistantAnswer(
        question="Czy moge odliczyc laptopa?",
        answer="To zalezy od profilu - sprawdz cytowane zrodla.",
        citations=["https://example.gov.pl/a"],
        model="test-model",
    )
    session.add(answer)
    session.commit()
    session.refresh(answer)
    assert answer.disclaimer_shown is True
    assert answer.created_at is not None
