"""Deterministic calculation engine: pure functions, golden values and the preview API.

Acceptance criteria covered here: scale thresholds and the tax-free amount, VAT threshold
below/equal/above, every ZUS variant, IP BOX eligibility, grosz rounding, the policy grep
for rate literals in ``calc.py``, and ``POST /api/calc/preview`` returning the result plus
the ``source_url`` of the parameters used.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import TaxProfile
from app.domain.enums import PitRegime, VatStatus, ZusVariant
from app.services import calc, tax_params

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "calc_golden_values.yaml"
CENT = Decimal("0.01")
MONEY_ZERO = Decimal("0.00")


def _money(value: object) -> Decimal:
    """Fixture/API money (decimal string) -> Decimal."""
    return Decimal(str(value))


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    """Golden values captured from the public MF/ZUS calculators (see the file header)."""
    loaded = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_fixture_documents_its_source_and_read_date(
    golden: dict[str, Any],
) -> None:
    for name in ("pit", "zus"):
        source = golden["source"][name]
        assert source["url"].startswith("https://")
        assert source["read_at"]
        assert source["name"]


# --------------------------------------------------------------------------- PIT scale


def test_pit_scale_matches_mf_golden_values(golden: dict[str, Any]) -> None:
    for case in golden["pit_scale"]:
        params = tax_params.load(case["year"])
        actual = calc.pit_scale(case["income"], params)
        assert actual == _money(case["tax"]), case


def test_pit_scale_thresholds_and_tax_free_amount_from_params() -> None:
    params = tax_params.load(2025)
    tax_free = params.pit.scale.tax_free_amount
    first_threshold = params.scale_thresholds[0]
    first_rate = params.pit.scale.rates[0].rate
    second_rate = params.pit.scale.rates[1].rate

    assert calc.pit_scale("-1", params) == MONEY_ZERO
    assert calc.pit_scale("0", params) == MONEY_ZERO
    assert calc.pit_scale(tax_free, params) == MONEY_ZERO
    assert calc.pit_scale(tax_free + 1, params) == _round(first_rate)

    at_threshold = _round((first_threshold - tax_free) * first_rate)
    assert calc.pit_scale(first_threshold, params) == at_threshold
    assert calc.pit_scale(first_threshold + 1, params) == _round(
        at_threshold + second_rate
    )


def test_pit_scale_accepts_decimal_int_and_string_inputs() -> None:
    params = tax_params.load(2025)

    assert calc.pit_scale(Decimal("100000"), params) == calc.pit_scale("100000", params)
    assert calc.pit_scale(100000, params) == calc.pit_scale("100000.00", params)


# -------------------------------------------------------------------------- PIT linear


def test_pit_linear_matches_golden_value(golden: dict[str, Any]) -> None:
    for case in golden["pit_linear"]:
        params = tax_params.load(case["year"])
        assert calc.pit_linear(case["income"], params) == _money(case["tax"]), case


def test_pit_linear_handles_zero_and_negative_income() -> None:
    params = tax_params.load(2025)

    assert calc.pit_linear("0", params) == MONEY_ZERO
    assert calc.pit_linear("-100", params) == MONEY_ZERO
    assert calc.pit_linear("-0.01", params) == MONEY_ZERO


# ----------------------------------------------------------------------- PIT lump sum


def test_pit_lump_sum_matches_golden_value(golden: dict[str, Any]) -> None:
    for case in golden["pit_lump_sum"]:
        params = tax_params.load(case["year"])
        rate = params.pit.lump_sum.rates_by_pkd[case["pkd"]]
        assert calc.pit_lump_sum(case["revenue"], rate, params) == _money(case["tax"]), case


def test_pit_lump_sum_rejects_a_rate_outside_the_parameter_document() -> None:
    params = tax_params.load(2025)

    with pytest.raises(calc.CalcInputError):
        calc.pit_lump_sum("100000", Decimal("1"), params)


def test_pit_lump_sum_handles_zero_negative_and_rounds_up() -> None:
    params = tax_params.load(2025)
    rate = params.pit.lump_sum.rates_by_pkd["4711"]

    assert calc.pit_lump_sum("0", rate, params) == MONEY_ZERO
    assert calc.pit_lump_sum("-100", rate, params) == MONEY_ZERO
    # 1.50 x the 3% rate is 0.045 PLN: ROUND_HALF_UP gives 0.05, HALF_EVEN would give 0.04.
    assert calc.pit_lump_sum("1.50", rate, params) == Decimal("0.05")


# --------------------------------------------------------------------------- IP BOX


def test_pit_ip_box_matches_golden_value(golden: dict[str, Any]) -> None:
    for case in golden["pit_ip_box"]:
        params = tax_params.load(case["year"])
        actual = calc.pit_ip_box(case["qualifying_income"], params, eligible=True)
        assert actual == _money(case["tax"]), case


def test_pit_ip_box_requires_eligibility() -> None:
    params = tax_params.load(2025)

    with pytest.raises(calc.IncompatibleRegimeError):
        calc.pit_ip_box("100000", params)


def test_pit_ip_box_zero_and_negative_qualifying_income() -> None:
    params = tax_params.load(2025)

    assert calc.pit_ip_box("0", params, eligible=True) == MONEY_ZERO
    assert calc.pit_ip_box("-100", params, eligible=True) == MONEY_ZERO


# ------------------------------------------------------------------------------ VAT


def test_vat_status_threshold_is_inclusive_below_equal_above() -> None:
    for year in (2025, 2026):
        params = tax_params.load(year)
        threshold = params.vat.exemption_threshold_pln

        assert calc.vat_status_check(threshold - CENT, params) is VatStatus.EXEMPT
        assert calc.vat_status_check(threshold, params) is VatStatus.EXEMPT
        assert calc.vat_status_check(threshold + CENT, params) is VatStatus.REGISTERED


def test_vat_status_handles_zero_and_negative_turnover() -> None:
    params = tax_params.load(2025)

    assert calc.vat_status_check("0", params) is VatStatus.EXEMPT
    assert calc.vat_status_check("-1", params) is VatStatus.EXEMPT


def test_vat_due_is_output_minus_input() -> None:
    params = tax_params.load(2025)
    top_rate = max(params.vat.rates)

    assert calc.vat_due([], [], params) == MONEY_ZERO
    assert (
        calc.vat_due(
            [calc.VatLine("10000.00", top_rate)],
            [calc.VatLine("4000.00", top_rate)],
            params,
        )
        == _money("1380.00")
    )


def test_vat_due_accepts_mappings_pairs_and_zero_rate_lines() -> None:
    params = tax_params.load(2025)
    top_rate = max(params.vat.rates)
    zero_rate = min(params.vat.rates)

    sales = [{"net": "1000.00", "rate": top_rate}, ("500.00", zero_rate)]
    due = calc.vat_due(sales, [], params)  # type: ignore[arg-type]

    assert due == _round(Decimal("1000.00") * top_rate)


def test_vat_due_negative_balance_is_a_credit() -> None:
    params = tax_params.load(2025)
    top_rate = max(params.vat.rates)

    due = calc.vat_due(
        [calc.VatLine("1000.00", top_rate)],
        [calc.VatLine("2000.00", top_rate)],
        params,
    )

    assert due == _round(Decimal("-1000.00") * top_rate)
    assert due < 0


def test_vat_due_allows_negative_correction_lines() -> None:
    params = tax_params.load(2025)
    top_rate = max(params.vat.rates)

    due = calc.vat_due([calc.VatLine("-1000.00", top_rate)], [], params)

    assert due == _round(Decimal("-1000.00") * top_rate)


def test_vat_due_rounds_each_line_half_up() -> None:
    params = tax_params.load(2025)
    low_rate = min(rate for rate in params.vat.rates if rate > 0)

    # 0.10 x the lowest non-zero rate is 0.005 PLN: ROUND_HALF_UP gives 0.01.
    assert calc.vat_due([("0.10", low_rate)], [], params) == Decimal("0.01")
    # 0.09 x the same rate is 0.0045 PLN and rounds down.
    assert calc.vat_due([("0.09", low_rate)], [], params) == MONEY_ZERO


def test_vat_due_rejects_an_unconfigured_rate() -> None:
    params = tax_params.load(2025)

    with pytest.raises(calc.CalcInputError):
        calc.vat_due([("100", Decimal("1"))], [], params)


# ------------------------------------------------------------------------------ ZUS


def test_zus_contributions_match_golden_values(golden: dict[str, Any]) -> None:
    for case in golden["zus"]:
        params = tax_params.load(case["year"])
        actual = calc.zus_contributions(case["month"], case["variant"], params)
        assert actual == _money(case["total"]), case


def test_zus_every_variant_is_supported_and_constant_within_the_year() -> None:
    params = tax_params.load(2025)

    for variant in ZusVariant:
        first = calc.zus_contributions(1, variant, params)
        last = calc.zus_contributions(12, variant, params)
        assert first == last
        assert first > 0


def test_zus_variant_data_comes_from_tax_year_params() -> None:
    params = tax_params.load(2025)

    assert calc.zus_contributions(1, ZusVariant.FULL, params) == params.zus.total_full_pln
    for variant, spec in params.zus.variants.items():
        assert calc.zus_contributions(1, variant, params) == spec.monthly_total_pln


def test_zus_rejects_invalid_month_and_unknown_variant() -> None:
    params = tax_params.load(2025)

    for month in (0, -1, 13):
        with pytest.raises(calc.CalcInputError):
            calc.zus_contributions(month, ZusVariant.FULL, params)
    with pytest.raises(calc.CalcInputError):
        calc.zus_contributions(1, "NOT_A_VARIANT", params)


def test_zus_reports_a_variant_missing_from_the_parameter_document() -> None:
    params = tax_params.load(2025)
    without_variants = params.model_copy(
        update={"zus": params.zus.model_copy(update={"variants": {}})}
    )

    with pytest.raises(calc.CalcInputError):
        calc.zus_contributions(1, ZusVariant.PREFERENTIAL_24M, without_variants)


# ---------------------------------------------------------- no hardcoded rates policy


def test_calc_module_contains_no_rate_or_threshold_literals() -> None:
    """The acceptance grep, scoped to the calculation engine."""
    text = (PROJECT_ROOT / "app" / "services" / "calc.py").read_text(encoding="utf-8")
    forbidden = (
        r"\b0\.(?:05|08|09|12|15|19|23)\b",
        r"\b200[ _]?000\b",
        r"\b300[ _]?000\b",
        r"\b66660\b",
        r"\b9\.76\b",
        r"\b(?:5|8|9|12|15|19|23)\s?%",
    )
    for pattern in forbidden:
        assert re.search(pattern, text) is None, pattern


# ------------------------------------------------------------------------- HTTP API


def _seed(session: Session, year: int) -> tax_params.TaxParams:
    tax_params.seed(session, year)
    session.commit()
    return tax_params.load(year)


def _profile(
    session: Session,
    *,
    regime: PitRegime = PitRegime.SCALE,
    zus_variant: ZusVariant = ZusVariant.FULL,
    ip_box_rate: Decimal | None = None,
    valid_from: date = date(2025, 1, 1),
    valid_to: date | None = None,
) -> TaxProfile:
    row = TaxProfile(
        pit_regime=regime,
        vat_status=VatStatus.REGISTERED,
        zus_variant=zus_variant,
        ip_box_rate=ip_box_rate,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    session.add(row)
    session.commit()
    return row


def test_api_preview_returns_result_and_source_url(
    api_client: TestClient, session: Session
) -> None:
    params = _seed(session, 2026)
    profile = _profile(session, valid_from=date(2026, 1, 1))
    top_rate = max(params.vat.rates)

    response = api_client.post(
        "/api/calc/preview",
        json={
            "year": 2026,
            "profile_id": profile.id,
            "inputs": {
                "income": "120000.00",
                "annual_turnover": "240000.00",
                "sales": [{"net": "10000.00", "rate": str(top_rate)}],
                "costs": [{"net": "4000.00", "rate": str(top_rate)}],
                "month": 1,
            },
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["year"] == 2026
    assert body["profile_id"] == profile.id
    assert body["source_url"] == str(params.source_url)
    assert body["result"]["pit"] == "10800.00"
    assert body["result"]["vat_status"] == "EXEMPT"
    assert body["result"]["vat_due"] == "1380.00"
    assert body["result"]["zus"] == "2198.35"


def test_api_preview_omits_items_without_inputs(
    api_client: TestClient, session: Session
) -> None:
    _seed(session, 2025)
    profile = _profile(session)

    response = api_client.post(
        "/api/calc/preview",
        json={"year": 2025, "profile_id": profile.id, "inputs": {"income": "30000"}},
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["pit"] == "0.00"
    assert result["vat_status"] is None
    assert result["vat_due"] is None
    assert result["zus"] is None


def test_api_preview_uses_the_profile_zus_variant(
    api_client: TestClient, session: Session
) -> None:
    _seed(session, 2026)
    profile = _profile(
        session,
        zus_variant=ZusVariant.PREFERENTIAL_24M,
        valid_from=date(2026, 1, 1),
    )

    response = api_client.post(
        "/api/calc/preview",
        json={
            "year": 2026,
            "profile_id": profile.id,
            "inputs": {"income": "0", "month": 3},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["zus"] == "782.03"


def test_api_preview_dispatches_linear_and_lump_sum(
    api_client: TestClient, session: Session
) -> None:
    params = _seed(session, 2025)
    linear = _profile(session, regime=PitRegime.LINEAR)
    lump_sum = _profile(session, regime=PitRegime.LUMP_SUM)

    linear_response = api_client.post(
        "/api/calc/preview",
        json={"year": 2025, "profile_id": linear.id, "inputs": {"income": "12345.67"}},
    )
    lump_rate = params.pit.lump_sum.rates_by_pkd["6201"]
    lump_response = api_client.post(
        "/api/calc/preview",
        json={
            "year": 2025,
            "profile_id": lump_sum.id,
            "inputs": {"revenue": "100000.00", "lump_sum_rate": str(lump_rate)},
        },
    )

    assert linear_response.status_code == 200, linear_response.text
    assert linear_response.json()["result"]["pit"] == "2345.68"
    assert lump_response.status_code == 200, lump_response.text
    assert lump_response.json()["result"]["pit"] == "12000.00"


def test_api_preview_ip_box_requires_profile_eligibility(
    api_client: TestClient, session: Session
) -> None:
    params = _seed(session, 2025)
    eligible = _profile(
        session, regime=PitRegime.IP_BOX, ip_box_rate=params.pit.ip_box.rate
    )
    ineligible = _profile(session, regime=PitRegime.IP_BOX)

    payload = {"year": 2025, "inputs": {"qualifying_income": "100000.00"}}
    allowed = api_client.post(
        "/api/calc/preview",
        json={**payload, "profile_id": eligible.id},
    )
    rejected = api_client.post(
        "/api/calc/preview",
        json={**payload, "profile_id": ineligible.id},
    )

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["result"]["pit"] == "5000.00"
    assert rejected.status_code == 409
    assert "eligib" in rejected.json()["detail"].lower()


def test_api_preview_validates_regime_inputs_rates_and_extra_keys(
    api_client: TestClient, session: Session
) -> None:
    _seed(session, 2025)
    profile = _profile(session)

    missing_income = api_client.post(
        "/api/calc/preview",
        json={"year": 2025, "profile_id": profile.id, "inputs": {"month": 1}},
    )
    unknown_rate = api_client.post(
        "/api/calc/preview",
        json={
            "year": 2025,
            "profile_id": profile.id,
            "inputs": {"income": "1000", "sales": [{"net": "10", "rate": "1"}]},
        },
    )
    extra_key = api_client.post(
        "/api/calc/preview",
        json={
            "year": 2025,
            "profile_id": profile.id,
            "inputs": {"income": "1000", "income_typo": "1"},
        },
    )

    assert missing_income.status_code == 422
    assert "income" in missing_income.json()["detail"]
    assert unknown_rate.status_code == 422
    assert extra_key.status_code == 422


def test_api_preview_missing_profile_or_parameters(
    api_client: TestClient, session: Session
) -> None:
    profile = _profile(session)  # parameters are deliberately not seeded

    unknown_profile = api_client.post(
        "/api/calc/preview",
        json={"year": 2025, "profile_id": 999, "inputs": {"income": "1000"}},
    )
    missing_params = api_client.post(
        "/api/calc/preview",
        json={"year": 2025, "profile_id": profile.id, "inputs": {"income": "1000"}},
    )

    assert unknown_profile.status_code == 404
    assert missing_params.status_code == 404
    assert "parameters" in missing_params.json()["detail"].lower()


def test_api_preview_rejects_a_profile_outside_the_year(
    api_client: TestClient, session: Session
) -> None:
    _seed(session, 2025)
    profile = _profile(session, valid_from=date(2024, 1, 1), valid_to=date(2024, 12, 31))

    response = api_client.post(
        "/api/calc/preview",
        json={"year": 2025, "profile_id": profile.id, "inputs": {"income": "1000"}},
    )

    assert response.status_code == 409
    assert "not in force" in response.json()["detail"]

