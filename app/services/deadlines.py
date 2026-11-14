"""Deadline calendar: generates obligations and keeps their status current.

Two sources of truth meet here:

* ``config/tax_params/<year>.yaml`` -> ``TaxParams.deadlines`` decides *which* obligations
  exist and *when* they fall due (``period end + offset_days``); no filing date is written
  in Python;
* ``tax_profiles`` decides *which of them apply* to the taxpayer (VAT registered or exempt,
  monthly or quarterly VAT settlement).

``OVERDUE`` is derived, never stored: an ``OPEN`` deadline becomes overdue the day after
its due date in Europe/Warsaw, so the answer does not depend on a nightly job. Generation
is idempotent - ``UNIQUE(kind, period, due_date)`` plus a lookup on that key make a second
run for the same year a no-op.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Deadline, TaxProfile
from app.domain.enums import (
    LEGACY_DEADLINE_STATUS_TO_CURRENT,
    DeadlineRule,
    DeadlineStatus,
    VatCadence,
    VatStatus,
)
from app.services import tax_params
from app.services.tax_params import DeadlineRuleSpec, TaxParams

__all__ = [
    "WARSAW",
    "GenerationResult",
    "active_profile",
    "derive_status",
    "generate_deadlines",
    "list_deadlines",
    "mark_deadline",
    "obligation_rules",
    "today_in_warsaw",
]

#: All tax dates are Polish dates, so "today" must be the Warsaw date, not UTC.
WARSAW: ZoneInfo = ZoneInfo("Europe/Warsaw")


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Outcome of one :func:`generate_deadlines` run (idempotency is observable)."""

    year: int
    created: int
    existing: int
    deadlines: tuple[Deadline, ...]


@dataclass(frozen=True, slots=True)
class _Period:
    """A settlement period: its label (``2025-03``, ``2025-Q1``, ``2025``) and end date."""

    label: str
    end: date


def today_in_warsaw(now: datetime | None = None) -> date:
    """Current date in Europe/Warsaw (naive datetimes are treated as Warsaw local time)."""
    if now is None:
        return datetime.now(WARSAW).date()
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(WARSAW).date()


def derive_status(
    stored: DeadlineStatus, due_date: date, *, as_of: date
) -> DeadlineStatus:
    """Status as of ``as_of``: ``DONE`` is final, anything else is overdue once late."""
    resolved = LEGACY_DEADLINE_STATUS_TO_CURRENT.get(stored, stored)
    if resolved is DeadlineStatus.DONE:
        return DeadlineStatus.DONE
    return DeadlineStatus.OVERDUE if due_date < as_of else DeadlineStatus.OPEN


def active_profile(session: Session, on: date) -> TaxProfile | None:
    """Profile covering ``on`` (latest ``valid_from`` wins when ranges overlap)."""
    return session.scalars(
        select(TaxProfile)
        .where(
            TaxProfile.valid_from <= on,
            or_(TaxProfile.valid_to.is_(None), TaxProfile.valid_to >= on),
        )
        .order_by(TaxProfile.valid_from.desc())
        .limit(1)
    ).first()


def obligation_rules(
    profile: TaxProfile, *, vat_cadence: VatCadence = VatCadence.MONTHLY
) -> tuple[DeadlineRule, ...]:
    """Rules that apply to ``profile``, in generation order."""
    rules: list[DeadlineRule] = []
    if profile.vat_status is VatStatus.REGISTERED:
        rules.append(
            DeadlineRule.VAT_QUARTERLY
            if vat_cadence is VatCadence.QUARTERLY
            else DeadlineRule.VAT_MONTHLY
        )
    rules.append(DeadlineRule.ZUS_DRA)
    rules.append(DeadlineRule.PIT_ADVANCE_MONTHLY)
    rules.append(DeadlineRule.PIT_ANNUAL)
    return tuple(rules)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _periods(rule: DeadlineRule, year: int) -> Iterator[_Period]:
    """Periods covered by ``rule`` in ``year`` (obligations *belonging to* the year)."""
    if rule in (
        DeadlineRule.VAT_MONTHLY,
        DeadlineRule.ZUS_DRA,
        DeadlineRule.PIT_ADVANCE_MONTHLY,
    ):
        for month in range(1, 13):
            yield _Period(f"{year}-{month:02d}", _month_end(year, month))
        return
    if rule is DeadlineRule.VAT_QUARTERLY:
        for quarter in range(1, 5):
            last_month = quarter * 3
            yield _Period(f"{year}-Q{quarter}", _month_end(year, last_month))
        return
    if rule is DeadlineRule.PIT_ANNUAL:
        # Settled the following year: the offset in the YAML carries it past 31 December.
        yield _Period(str(year), date(year, 12, 31))
        return
    raise tax_params.TaxParamsConfigError(f"unsupported deadline rule: {rule}")


def _wanted_specs(
    params: TaxParams, profile: TaxProfile, vat_cadence: VatCadence
) -> Sequence[DeadlineRuleSpec]:
    return [params.rule(rule) for rule in obligation_rules(profile, vat_cadence=vat_cadence)]



def generate_deadlines(
    session: Session,
    year: int,
    *,
    profile: TaxProfile,
    vat_cadence: VatCadence = VatCadence.MONTHLY,
    params: TaxParams | None = None,
) -> GenerationResult:
    """Materialise the obligations of ``year`` for ``profile`` (idempotent).

    ``params`` defaults to the shipped parameters of ``year``. Existing rows are left
    untouched - including their status and notes - so re-running the generator never
    resurrects a completed obligation or overwrites a user note.
    """
    resolved = params or tax_params.load(year)
    specs = _wanted_specs(resolved, profile, vat_cadence)

    created = 0
    existing = 0
    generated: list[Deadline] = []
    note = f"generated from {resolved.year} v{resolved.version}"

    for spec in specs:
        for period in _periods(spec.rule, year):
            due_date = period.end + timedelta(days=spec.offset_days)
            row = session.scalars(
                select(Deadline).where(
                    Deadline.kind == spec.kind,
                    Deadline.period == period.label,
                    Deadline.due_date == due_date,
                )
            ).first()
            if row is None:
                row = Deadline(
                    kind=spec.kind,
                    period=period.label,
                    due_date=due_date,
                    status=DeadlineStatus.OPEN,
                    notes=f"{note} rule={spec.rule.value}",
                )
                session.add(row)
                created += 1
            else:
                existing += 1
            generated.append(row)

    session.flush()
    generated.sort(
        key=lambda deadline: (deadline.due_date, deadline.kind.value, deadline.period)
    )
    return GenerationResult(
        year=year, created=created, existing=existing, deadlines=tuple(generated)
    )


def list_deadlines(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    status: DeadlineStatus | None = None,
    as_of: date | None = None,
) -> list[Deadline]:
    """Deadlines within ``[date_from, date_to]``, optionally filtered by derived status."""
    today = as_of or today_in_warsaw()
    statement = select(Deadline).order_by(Deadline.due_date, Deadline.kind, Deadline.period)
    if date_from is not None:
        statement = statement.where(Deadline.due_date >= date_from)
    if date_to is not None:
        statement = statement.where(Deadline.due_date <= date_to)
    rows = list(session.scalars(statement))
    if status is None:
        return rows
    return [
        row for row in rows if derive_status(row.status, row.due_date, as_of=today) is status
    ]


def mark_deadline(
    session: Session,
    deadline_id: int,
    *,
    status: DeadlineStatus | None = None,
    notes: str | None = None,
) -> Deadline | None:
    """Update a deadline; returns ``None`` when the id does not exist."""
    row = session.get(Deadline, deadline_id)
    if row is None:
        return None
    if status is not None:
        row.status = status
    if notes is not None:
        row.notes = notes
    session.flush()
    return row
