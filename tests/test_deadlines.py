"""Deadline calendar and decision log tests.

Acceptance criteria covered here: deadlines are generated for a VAT+scale profile, the run
is idempotent, a deadline becomes OVERDUE after its due date (Europe/Warsaw), and the
decisions endpoints work.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Deadline, Decision, TaxProfile
from app.domain.enums import (
    DeadlineKind,
    DeadlineRule,
    DeadlineStatus,
    PitRegime,
    VatCadence,
    VatStatus,
    ZusVariant,
)
from app.services import deadlines as deadlines_service
from app.services import tax_params

#: Year used by the generation tests (a monthly-VAT sole proprietor on the PIT scale).
VAT_SCALE_YEAR = 2025


@pytest.fixture
def profile(session: Session) -> TaxProfile:
    row = TaxProfile(
        pit_regime=PitRegime.SCALE,
        vat_status=VatStatus.REGISTERED,
        zus_variant=ZusVariant.FULL,
        valid_from=date(VAT_SCALE_YEAR, 1, 1),
    )
    session.add(row)
    session.commit()
    return row


def _generate(
    session: Session,
    profile: TaxProfile,
    *,
    year: int = VAT_SCALE_YEAR,
    vat_cadence: VatCadence = VatCadence.MONTHLY,
) -> deadlines_service.GenerationResult:
    result = deadlines_service.generate_deadlines(
        session, year, profile=profile, vat_cadence=vat_cadence
    )
    session.commit()
    return result


def _deadline(**overrides: object) -> Deadline:
    payload: dict[str, object] = {
        "kind": DeadlineKind.VAT_DECLARATION,
        "period": "2026-01",
        "due_date": date(2026, 2, 25),
        "status": DeadlineStatus.OPEN,
    }
    payload.update(overrides)
    return Deadline(**payload)  # type: ignore[arg-type]


# ------------------------------------------------------------------------- generation


def test_generates_vat_zus_advances_and_annual_pit(
    session: Session, profile: TaxProfile
) -> None:
    result = _generate(session, profile)

    # 12 monthly VAT + 12 ZUS DRA + 12 PIT advances + 1 annual PIT.
    assert result.created == 37
    assert result.existing == 0
    kinds = Counter(deadline.kind for deadline in result.deadlines)
    assert kinds[DeadlineKind.VAT_DECLARATION] == 12
    assert kinds[DeadlineKind.ZUS_CONTRIBUTION] == 12
    assert kinds[DeadlineKind.PIT_ADVANCE] == 12
    assert kinds[DeadlineKind.PIT_ANNUAL] == 1
    assert {deadline.status for deadline in result.deadlines} == {DeadlineStatus.OPEN}

    due = {(deadline.kind, deadline.period): deadline.due_date for deadline in result.deadlines}
    # Due dates come from the YAML offsets, not from code.
    assert due[(DeadlineKind.VAT_DECLARATION, "2025-01")] == date(2025, 2, 25)
    assert due[(DeadlineKind.ZUS_CONTRIBUTION, "2025-01")] == date(2025, 2, 20)
    assert due[(DeadlineKind.PIT_ADVANCE, "2025-01")] == date(2025, 2, 20)
    assert due[(DeadlineKind.VAT_DECLARATION, "2025-12")] == date(2026, 1, 25)
    # The annual return for 2025 is settled the following year.
    assert due[(DeadlineKind.PIT_ANNUAL, "2025")] == date(2026, 4, 30)


def test_generation_is_idempotent(session: Session, profile: TaxProfile) -> None:
    first = _generate(session, profile)
    second = _generate(session, profile)

    assert second.created == 0
    assert second.existing == first.created
    assert [deadline.id for deadline in second.deadlines] == [
        deadline.id for deadline in first.deadlines
    ]
    assert session.scalar(select(func.count()).select_from(Deadline)) == first.created


def test_regeneration_keeps_a_completed_status(session: Session, profile: TaxProfile) -> None:
    first = _generate(session, profile)
    done = first.deadlines[0]
    done.status = DeadlineStatus.DONE
    session.commit()

    _generate(session, profile)

    session.refresh(done)
    assert done.status is DeadlineStatus.DONE


def test_quarterly_vat_uses_quarter_periods(session: Session, profile: TaxProfile) -> None:
    result = _generate(session, profile, vat_cadence=VatCadence.QUARTERLY)

    vat = [row for row in result.deadlines if row.kind is DeadlineKind.VAT_DECLARATION]
    assert [row.period for row in vat] == ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"]
    assert vat[0].due_date == date(2025, 4, 25)
    assert result.created == 29


def test_vat_exempt_profile_has_no_vat_deadlines(session: Session) -> None:
    exempt = TaxProfile(
        pit_regime=PitRegime.SCALE,
        vat_status=VatStatus.EXEMPT,
        zus_variant=ZusVariant.ULGA_NA_START,
        valid_from=date(VAT_SCALE_YEAR, 1, 1),
    )
    session.add(exempt)
    session.commit()

    result = _generate(session, exempt)

    assert DeadlineKind.VAT_DECLARATION not in {row.kind for row in result.deadlines}
    assert result.created == 25


def test_missing_rule_is_reported(session: Session, profile: TaxProfile) -> None:
    params = tax_params.load(VAT_SCALE_YEAR)
    without_annual = params.model_copy(
        update={
            "deadlines": [
                spec for spec in params.deadlines if spec.rule is not DeadlineRule.PIT_ANNUAL
            ]
        }
    )

    with pytest.raises(tax_params.TaxParamsConfigError) as excinfo:
        deadlines_service.generate_deadlines(
            session, VAT_SCALE_YEAR, profile=profile, params=without_annual
        )

    assert DeadlineRule.PIT_ANNUAL.value in str(excinfo.value)


# ----------------------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("stored", "due_date", "as_of", "expected"),
    [
        (DeadlineStatus.OPEN, date(2025, 2, 25), date(2025, 2, 25), DeadlineStatus.OPEN),
        (DeadlineStatus.OPEN, date(2025, 2, 25), date(2025, 2, 26), DeadlineStatus.OVERDUE),
        (DeadlineStatus.DONE, date(2025, 2, 25), date(2025, 3, 1), DeadlineStatus.DONE),
        (DeadlineStatus.PENDING, date(2025, 2, 25), date(2025, 2, 20), DeadlineStatus.OPEN),
        (DeadlineStatus.MISSED, date(2025, 2, 25), date(2025, 3, 1), DeadlineStatus.OVERDUE),
    ],
)
def test_derive_status(
    stored: DeadlineStatus, due_date: date, as_of: date, expected: DeadlineStatus
) -> None:
    assert deadlines_service.derive_status(stored, due_date, as_of=as_of) is expected


def test_today_in_warsaw_converts_from_utc() -> None:
    # 2025-12-31 23:30 UTC is already 2026-01-01 in Warsaw.
    moment = datetime(2025, 12, 31, 23, 30, tzinfo=UTC)

    assert deadlines_service.today_in_warsaw(moment) == date(2026, 1, 1)


def test_active_profile_picks_the_latest_covering_row(session: Session) -> None:
    session.add_all(
        [
            TaxProfile(
                pit_regime=PitRegime.LINEAR,
                vat_status=VatStatus.EXEMPT,
                zus_variant=ZusVariant.FULL,
                valid_from=date(2020, 1, 1),
            ),
            TaxProfile(
                pit_regime=PitRegime.SCALE,
                vat_status=VatStatus.REGISTERED,
                zus_variant=ZusVariant.FULL,
                valid_from=date(2026, 1, 1),
            ),
        ]
    )
    session.commit()

    older = deadlines_service.active_profile(session, date(2025, 12, 31))
    assert older is not None
    assert older.pit_regime is PitRegime.LINEAR
    newer = deadlines_service.active_profile(session, date(2026, 6, 30))
    assert newer is not None
    assert newer.vat_status is VatStatus.REGISTERED


# -------------------------------------------------------------------------- HTTP: list


def _seed_register(session: Session) -> dict[str, int]:
    """Three deadlines: one long overdue, one open far in the future, one done."""
    overdue = _deadline(period="2001-01", due_date=date(2001, 1, 20), status=DeadlineStatus.OPEN)
    upcoming = _deadline(
        period="2999-01",
        due_date=date(2999, 1, 20),
        kind=DeadlineKind.PIT_ADVANCE,
        status=DeadlineStatus.OPEN,
    )
    done = _deadline(
        period="2001-02",
        due_date=date(2001, 2, 20),
        kind=DeadlineKind.ZUS_CONTRIBUTION,
        status=DeadlineStatus.DONE,
    )
    session.add_all([overdue, upcoming, done])
    session.commit()
    return {"overdue": overdue.id, "upcoming": upcoming.id, "done": done.id}


def test_api_lists_deadlines_with_derived_status(
    api_client: TestClient, session: Session
) -> None:
    ids = _seed_register(session)

    response = api_client.get("/api/deadlines")

    assert response.status_code == 200
    body = {row["id"]: row for row in response.json()}
    assert body[ids["overdue"]]["status"] == DeadlineStatus.OVERDUE.value
    assert body[ids["upcoming"]]["status"] == DeadlineStatus.OPEN.value
    assert body[ids["done"]]["status"] == DeadlineStatus.DONE.value
    assert [row["due_date"] for row in response.json()] == [
        "2001-01-20",
        "2001-02-20",
        "2999-01-20",
    ]


def test_api_filters_deadlines(api_client: TestClient, session: Session) -> None:
    ids = _seed_register(session)

    overdue = api_client.get("/api/deadlines", params={"status": "OVERDUE"}).json()
    assert [row["id"] for row in overdue] == [ids["overdue"]]

    window = api_client.get(
        "/api/deadlines", params={"from": "2001-02-01", "to": "2001-12-31"}
    ).json()
    assert [row["id"] for row in window] == [ids["done"]]

    # Legacy-only statuses are not part of the public filter anymore.
    assert api_client.get("/api/deadlines", params={"status": "PENDING"}).status_code == 422


def test_api_marks_a_deadline_done(api_client: TestClient, session: Session) -> None:
    ids = _seed_register(session)

    response = api_client.patch(
        f"/api/deadlines/{ids['overdue']}",
        json={"status": "DONE", "notes": "zaplacone i wyslane"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == DeadlineStatus.DONE.value
    assert body["notes"] == "zaplacone i wyslane"
    stored = session.get(Deadline, ids["overdue"])
    assert stored is not None
    assert stored.status is DeadlineStatus.DONE

    done = api_client.get("/api/deadlines", params={"status": "DONE"}).json()
    assert ids["overdue"] in {row["id"] for row in done}


def test_api_patch_rejects_unknown_and_legacy_input(
    api_client: TestClient, session: Session
) -> None:
    ids = _seed_register(session)

    assert api_client.patch("/api/deadlines/424242", json={"status": "DONE"}).status_code == 404
    # Legacy statuses are readable but must not be written.
    legacy = api_client.patch(f"/api/deadlines/{ids['done']}", json={"status": "WAIVED"})
    assert legacy.status_code == 422
    assert api_client.patch(f"/api/deadlines/{ids['done']}", json={}).status_code == 422


def test_api_generates_deadlines_idempotently(
    api_client: TestClient, profile: TaxProfile
) -> None:
    first = api_client.post("/api/deadlines/generate", json={"year": VAT_SCALE_YEAR})

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] == 37
    assert body["existing"] == 0
    assert len(body["deadlines"]) == 37

    second = api_client.post(
        "/api/deadlines/generate",
        json={"year": VAT_SCALE_YEAR, "vat_cadence": "quarterly"},
    )
    assert second.status_code == 200, second.text
    # Monthly rows already exist; only the four quarterly VAT rows are new.
    assert second.json()["created"] == 4
    assert second.json()["existing"] == 25


def test_api_generate_requires_a_profile(api_client: TestClient) -> None:
    response = api_client.post("/api/deadlines/generate", json={"year": 2026})

    assert response.status_code == 409
    assert "profile" in response.json()["detail"].lower()




# --------------------------------------------------------------------- HTTP: decisions


def test_decision_crud(api_client: TestClient, session: Session) -> None:
    payload = {
        "topic": "vat_registration",
        "choice": "stay_exempt",
        "rationale": "Przewidywany obrot ponizej limitu zwolnienia.",
        "source_refs": [
            "https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU20040540535",
            "https://www.zus.pl/baza-wiedzy/skladki-wskazniki-odsetki/skladki",
        ],
        "decided_at": "2026-01-02T09:30:00+00:00",
    }

    created = api_client.post("/api/decisions", json=payload)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["topic"] == payload["topic"]
    assert body["source_refs"] == payload["source_refs"]
    assert body["decided_at"].startswith("2026-01-02T09:30:00")

    listed = api_client.get("/api/decisions", params={"topic": "vat_registration"})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [body["id"]]
    assert api_client.get("/api/decisions", params={"topic": "other"}).json() == []

    stored = session.scalars(select(Decision)).one()
    assert stored.source_refs == payload["source_refs"]


def test_decision_defaults_to_now(api_client: TestClient) -> None:
    response = api_client.post("/api/decisions", json={"topic": "zus_variant", "choice": "FULL"})

    assert response.status_code == 201
    body = response.json()
    assert body["source_refs"] == []
    # SQLite drops the offset, so compare as naive UTC values.
    decided = datetime.fromisoformat(body["decided_at"]).replace(tzinfo=None)
    naive_utc_now = datetime.now(UTC).replace(tzinfo=None)
    assert abs((naive_utc_now - decided).total_seconds()) < 300


def test_decision_requires_http_source_refs(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/decisions",
        json={"topic": "ip_box", "choice": "IP_BOX", "source_refs": ["not-a-url"]},
    )

    assert response.status_code == 422


def test_decision_log_is_append_only(api_client: TestClient) -> None:
    payload = {
        "topic": "vat_registration",
        "choice": "register",
        "decided_at": "2026-02-01T08:00:00+00:00",
    }
    assert api_client.post("/api/decisions", json=payload).status_code == 201

    duplicate = api_client.post("/api/decisions", json=payload)

    assert duplicate.status_code == 409
    assert "append-only" in duplicate.json()["detail"]
