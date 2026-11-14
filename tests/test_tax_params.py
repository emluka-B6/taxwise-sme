"""Tax parameters: loading, validation, seeding and the HTTP endpoint.

Acceptance criteria covered here: the 2025/2026 files load, an invalid file is rejected,
seeding is idempotent, the endpoint returns ``source_url`` and ``effective_from``, and a
missing year is a 404.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import TaxYearParams as TaxYearParamsRow
from app.domain.enums import DeadlineKind, DeadlineRule
from app.services import tax_params

PARAMS_DIR = Path(__file__).resolve().parents[1] / "config" / "tax_params"


def _document(year: int) -> dict[str, object]:
    """Raw YAML of a shipped year, as a mutable dict for negative tests."""
    return yaml.safe_load((PARAMS_DIR / f"{year}.yaml").read_text(encoding="utf-8"))


def _write(tmp_path: Path, document: object, name: str = "params.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- loading


def test_loads_2025() -> None:
    params = tax_params.load(2025)

    assert params.year == 2025
    assert params.version == 1
    assert params.effective_from == date(2025, 1, 1)
    assert str(params.source_url).startswith("https://")
    assert params.notes
    # PIT scale: thresholds ascend and the top bracket is open-ended.
    assert params.scale_thresholds == (Decimal("120000"),)
    assert params.pit.scale.rates[-1].threshold is None
    assert params.pit.scale.tax_free_amount == Decimal("30000")
    assert params.pit.ip_box.rate == Decimal("0.05")
    assert params.pit.lump_sum.rates_by_pkd["6201"] == Decimal("0.12")
    # ZUS components add up to the declared total.
    computed = sum(
        (component.base * component.rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
        for component in params.zus.components
    )
    assert computed == params.zus.total_full_pln
    assert {spec.rule for spec in params.deadlines} == set(DeadlineRule)


def test_loads_2026_and_differs_from_2025() -> None:
    params = tax_params.load(2026)

    assert params.year == 2026
    assert params.effective_from == date(2026, 1, 1)
    assert params.vat.exemption_threshold_pln == Decimal("240000")
    assert params.vat.exemption_threshold_pln != tax_params.load(2025).vat.exemption_threshold_pln
    assert DeadlineKind.PIT_ANNUAL in {spec.kind for spec in params.deadlines}


def test_every_shipped_file_is_valid_and_matches_its_name() -> None:
    files = tax_params.iter_config_files()
    assert {path.stem for path in files} >= {"2025", "2026"}
    for path in files:
        params = tax_params.load_file(path)
        assert str(params.year) == path.stem
        assert params.effective_from.year == params.year
        assert params.source_url.path not in (None, "", "/")


def test_load_is_cached_per_year() -> None:
    tax_params.clear_cache()
    assert tax_params.load(2026) is tax_params.load(2026)


def test_unknown_year_is_rejected() -> None:
    with pytest.raises(tax_params.TaxParamsNotFound):
        tax_params.load(2019)


def test_load_rejects_a_file_declaring_another_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Content says 2026 while the file is asked for as 2027.
    document = _document(2026)
    _write(tmp_path, document, name="2027.yaml")
    monkeypatch.setattr(tax_params, "CONFIG_DIR", tmp_path)
    tax_params.clear_cache()
    try:
        with pytest.raises(tax_params.TaxParamsConfigError):
            tax_params.load(2027)
    finally:
        tax_params.clear_cache()



# ------------------------------------------------------------------------ validation


def test_inconsistent_zus_total_is_rejected(tmp_path: Path) -> None:
    document = _document(2025)
    zus = document["zus"]
    assert isinstance(zus, dict)
    zus["total_full_pln"] = 1000

    with pytest.raises(tax_params.TaxParamsConfigError) as excinfo:
        tax_params.load_file(_write(tmp_path, document))

    assert "total_full_pln" in str(excinfo.value)


def test_non_increasing_thresholds_are_rejected(tmp_path: Path) -> None:
    document = _document(2025)
    pit = document["pit"]
    assert isinstance(pit, dict)
    scale = pit["scale"]
    assert isinstance(scale, dict)
    scale["rates"] = [
        {"threshold": 120000, "rate": 0.12},
        {"threshold": 100000, "rate": 0.32},
    ]

    with pytest.raises(tax_params.TaxParamsConfigError) as excinfo:
        tax_params.load_file(_write(tmp_path, document))

    assert "thresholds" in str(excinfo.value)


def test_negative_rate_is_rejected(tmp_path: Path) -> None:
    document = _document(2025)
    pit = document["pit"]
    assert isinstance(pit, dict)
    ip_box = pit["ip_box"]
    assert isinstance(ip_box, dict)
    ip_box["rate"] = -1

    with pytest.raises(tax_params.TaxParamsConfigError):
        tax_params.load_file(_write(tmp_path, document))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    document = _document(2025)
    vat = document["vat"]
    assert isinstance(vat, dict)
    vat["exemption_treshold_pln"] = 1  # typo -> must not be silently ignored

    with pytest.raises(tax_params.TaxParamsConfigError):
        tax_params.load_file(_write(tmp_path, document))


def test_effective_from_outside_the_year_is_rejected(tmp_path: Path) -> None:
    document = _document(2025)
    document["effective_from"] = "2026-01-01"

    with pytest.raises(tax_params.TaxParamsConfigError) as excinfo:
        tax_params.load_file(_write(tmp_path, document))

    assert "effective_from" in str(excinfo.value)


def test_rule_kind_mismatch_is_rejected(tmp_path: Path) -> None:
    document = _document(2025)
    deadlines = document["deadlines"]
    assert isinstance(deadlines, list)
    deadlines[0]["kind"] = "PIT_ANNUAL"

    with pytest.raises(tax_params.TaxParamsConfigError) as excinfo:
        tax_params.load_file(_write(tmp_path, document))

    assert "rule/kind mismatch" in str(excinfo.value)


# ----------------------------------------------------------------------- persistence


def test_seed_is_idempotent(session: Session) -> None:
    first, first_created = tax_params.seed(session, 2026)
    session.commit()
    second, second_created = tax_params.seed(session, 2026)
    session.commit()

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert session.scalar(select(func.count()).select_from(TaxYearParamsRow)) == 1
    assert second.source_url == str(tax_params.load(2026).source_url)
    assert "year" not in second.payload  # header fields live in columns, not in the payload


def test_stored_payload_round_trips(session: Session) -> None:
    params = tax_params.load(2025)
    row, _ = tax_params.upsert(session, params)

    assert tax_params.from_row(row) == params
    assert row.payload["zus"]["total_full_pln"] == str(params.zus.total_full_pln)


def test_a_new_version_adds_a_row_and_wins(session: Session) -> None:
    tax_params.upsert(session, tax_params.load(2026))
    tax_params.upsert(session, tax_params.load(2026).model_copy(update={"version": 2}))
    session.commit()

    assert session.scalar(select(func.count()).select_from(TaxYearParamsRow)) == 2
    stored = tax_params.latest(session, 2026)
    assert stored is not None
    assert stored.version == 2


# ------------------------------------------------------------------------------- API


def test_api_returns_source_url_and_effective_from(
    api_client: TestClient, session: Session
) -> None:
    tax_params.seed(session, 2026)
    session.commit()

    response = api_client.get("/api/tax-params/2026")

    assert response.status_code == 200
    body = response.json()
    assert body["year"] == 2026
    assert body["version"] == 1
    assert body["source_url"] == str(tax_params.load(2026).source_url)
    assert body["effective_from"] == "2026-01-01"
    assert Decimal(str(body["vat"]["exemption_threshold_pln"])) == Decimal("240000")
    assert Decimal(str(body["pit"]["scale"]["rates"][0]["threshold"])) == Decimal("120000")


def test_api_serves_the_highest_version(api_client: TestClient, session: Session) -> None:
    tax_params.upsert(session, tax_params.load(2026))
    tax_params.upsert(session, tax_params.load(2026).model_copy(update={"version": 3}))
    session.commit()

    body = api_client.get("/api/tax-params/2026").json()

    assert body["version"] == 3


def test_api_returns_404_for_a_missing_year(api_client: TestClient) -> None:
    response = api_client.get("/api/tax-params/2019")

    assert response.status_code == 404
    assert "2019" in response.json()["detail"]


def test_api_validates_the_year_path(api_client: TestClient) -> None:
    assert api_client.get("/api/tax-params/999").status_code == 422
