"""Versioned tax parameters: YAML documents -> validated models -> ``tax_year_params`` rows.

Rates, thresholds and contribution bases live in ``config/tax_params/<year>.yaml``. This
module knows only the *shape* of such a document and the invariants that must hold for it
to be usable; it never contains a Polish rate or threshold itself. The validated document
is persisted in ``tax_year_params`` (keyed by ``year`` + ``version``, with ``source_url``
and ``effective_from`` next to the payload) and served by ``GET /api/tax-params/{year}``.

Loading is cached per year; :func:`clear_cache` drops the cache after an edit in a long
running process (the seed script always reads from disk in a fresh process).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path
from typing import Annotated, Final, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TaxYearParams as TaxYearParamsRow
from app.domain.enums import DeadlineKind, DeadlineRule

__all__ = [
    "CONFIG_DIR",
    "RULE_KINDS",
    "ZUS_TOTAL_TOLERANCE_PLN",
    "DeadlineRuleSpec",
    "Pit",
    "PitIpBox",
    "PitLinear",
    "PitLumpSum",
    "PitScale",
    "ScaleRate",
    "TaxParams",
    "TaxParamsConfigError",
    "TaxParamsError",
    "TaxParamsNotFound",
    "Vat",
    "Zus",
    "ZusComponent",
    "clear_cache",
    "config_path",
    "from_row",
    "iter_config_files",
    "latest",
    "load",
    "load_file",
    "seed",
    "upsert",
]

#: Where shipped parameter files live (``<repo>/config/tax_params``).
CONFIG_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "config" / "tax_params"

#: Rounding difference tolerated between ``zus.components`` and ``zus.total_full_pln``.
ZUS_TOTAL_TOLERANCE_PLN: Final[Decimal] = Decimal("0.01")

_CENT: Final[Decimal] = Decimal("0.01")

#: Document keys stored in dedicated columns instead of the JSON payload.
_HEADER_FIELDS: Final[frozenset[str]] = frozenset(
    {"year", "version", "source_url", "effective_from"}
)

#: A rate is stored as a decimal fraction between zero and one, never as a percentage.
type _Rate = Annotated[Decimal, Field(gt=0, le=1)]
type _NonNegativeRate = Annotated[Decimal, Field(ge=0, le=1)]
type _PositiveMoney = Annotated[Decimal, Field(gt=0)]

_PKD_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{4}")

#: Kind each rule may produce; guards against a typo in the YAML ``kind`` field.
RULE_KINDS: Final[Mapping[DeadlineRule, DeadlineKind]] = {
    DeadlineRule.VAT_MONTHLY: DeadlineKind.VAT_DECLARATION,
    DeadlineRule.VAT_QUARTERLY: DeadlineKind.VAT_DECLARATION,
    DeadlineRule.ZUS_DRA: DeadlineKind.ZUS_CONTRIBUTION,
    DeadlineRule.PIT_ADVANCE_MONTHLY: DeadlineKind.PIT_ADVANCE,
    DeadlineRule.PIT_ANNUAL: DeadlineKind.PIT_ANNUAL,
}


class TaxParamsError(Exception):
    """Base class for parameter-configuration problems."""


class TaxParamsNotFound(TaxParamsError, FileNotFoundError):
    """No parameter file (or no stored row) exists for the requested year."""


class TaxParamsConfigError(TaxParamsError, ValueError):
    """A parameter document is missing, unparsable or violates an invariant."""


class _Model(BaseModel):
    """Strict base: unknown YAML keys are typos and must fail loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScaleRate(_Model):
    """One PIT scale bracket: income up to ``threshold`` is taxed at ``rate``."""

    threshold: Annotated[Decimal, Field(gt=0)] | None = Field(
        default=None,
        description="Upper bound of the bracket; None for the open-ended top bracket.",
    )
    rate: _Rate


class PitScale(_Model):
    """Progressive PIT scale with a tax-free amount."""

    tax_free_amount: Annotated[Decimal, Field(ge=0)]
    rates: Annotated[list[ScaleRate], Field(min_length=1)]

    @model_validator(mode="after")
    def _thresholds_increase(self) -> Self:
        thresholds = [rate.threshold for rate in self.rates if rate.threshold is not None]
        if len(thresholds) != len(self.rates) and self.rates[-1].threshold is not None:
            raise ValueError("only the last scale bracket may omit its threshold")
        if thresholds != sorted(set(thresholds)):
            raise ValueError(f"scale thresholds must increase strictly, got {thresholds}")
        return self


class PitLinear(_Model):
    """Flat-rate PIT."""

    rate: _Rate


class PitLumpSum(_Model):
    """Lump sum on registered revenue ("ryczalt"), keyed by PKD 2007 class."""

    rates_by_pkd: Annotated[dict[str, _Rate], Field(min_length=1)]

    @model_validator(mode="after")
    def _pkd_codes(self) -> Self:
        invalid = [code for code in self.rates_by_pkd if not _PKD_PATTERN.fullmatch(code)]
        if invalid:
            raise ValueError(f"lump-sum keys must be 4-digit PKD codes, got {invalid}")
        return self


class PitIpBox(_Model):
    """IP BOX rate for qualified intellectual-property income."""

    rate: _Rate


class Pit(_Model):
    """Personal income tax variants available to a sole proprietor."""

    scale: PitScale
    linear: PitLinear
    lump_sum: PitLumpSum
    ip_box: PitIpBox


class Vat(_Model):
    """VAT registration threshold and the rates in force."""

    exemption_threshold_pln: _PositiveMoney
    rates: Annotated[list[_NonNegativeRate], Field(min_length=1)]

    @model_validator(mode="after")
    def _rates_are_distinct_and_sorted(self) -> Self:
        if self.rates != sorted(set(self.rates)):
            raise ValueError(f"VAT rates must be distinct and ascending, got {self.rates}")
        return self


class ZusComponent(_Model):
    """One contribution: ``base`` (PLN) x ``rate`` (fraction)."""

    name: Annotated[str, Field(min_length=1, max_length=64)]
    base: _PositiveMoney
    rate: _Rate


class Zus(_Model):
    """Contribution components and the monthly total they must add up to."""

    components: Annotated[list[ZusComponent], Field(min_length=1)]
    total_full_pln: _PositiveMoney

    @model_validator(mode="after")
    def _total_matches_components(self) -> Self:
        computed = sum(
            (
                (component.base * component.rate).quantize(_CENT, ROUND_HALF_UP)
                for component in self.components
            ),
            Decimal("0"),
        )
        if abs(computed - self.total_full_pln) > ZUS_TOTAL_TOLERANCE_PLN:
            raise ValueError(
                "zus.total_full_pln does not match the components: "
                f"components sum to {computed}, total says {self.total_full_pln}"
            )
        return self


class DeadlineRuleSpec(_Model):
    """One entry of the filing calendar.

    ``offset_days`` is counted from the end of the obligation period, which keeps the
    calendar declarative and calendar-system neutral (January VAT -> 2025-01-31 + offset).
    """

    kind: DeadlineKind
    rule: DeadlineRule
    offset_days: Annotated[int, Field(ge=0, le=366)]


class TaxParams(_Model):
    """A whole ``config/tax_params/<year>.yaml`` document."""

    year: Annotated[int, Field(ge=2000, le=2100)]
    version: Annotated[int, Field(ge=1)]
    source_url: HttpUrl
    effective_from: date
    notes: Annotated[str, Field(min_length=1)]
    pit: Pit
    vat: Vat
    zus: Zus
    deadlines: Annotated[list[DeadlineRuleSpec], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_document(self) -> Self:
        if self.effective_from.year != self.year:
            raise ValueError(
                f"effective_from {self.effective_from} must fall in the file's year {self.year}"
            )
        if self.source_url.path in (None, "", "/"):
            raise ValueError(
                f"source_url must point at a specific document, not a site root: {self.source_url}"
            )
        rules = [spec.rule for spec in self.deadlines]
        if len(rules) != len(set(rules)):
            raise ValueError(f"deadline rules must be unique, got {rules}")
        mismatched = [
            spec.rule for spec in self.deadlines if RULE_KINDS[spec.rule] is not spec.kind
        ]
        if mismatched:
            raise ValueError(
                f"rule/kind mismatch: {[rule.value for rule in mismatched]} must generate "
                f"{[RULE_KINDS[rule].value for rule in mismatched]}"
            )
        return self

    def rule(self, rule: DeadlineRule) -> DeadlineRuleSpec:
        """Return the specification of ``rule`` or raise :class:`TaxParamsConfigError`."""
        for spec in self.deadlines:
            if spec.rule is rule:
                return spec
        raise TaxParamsConfigError(
            f"{self.year} v{self.version} has no deadline rule {rule.value!r} "
            f"(source: {self.source_url})"
        )

    @property
    def scale_thresholds(self) -> tuple[Decimal, ...]:
        """Explicit (non open-ended) scale thresholds, ascending."""
        return tuple(
            rate.threshold for rate in self.pit.scale.rates if rate.threshold is not None
        )





def config_path(year: int, *, config_dir: Path | None = None) -> Path:
    """Return the parameter file for ``year`` and fail when it is not shipped."""
    path = (config_dir or CONFIG_DIR) / f"{year}.yaml"
    if not path.is_file():
        raise TaxParamsNotFound(f"no tax parameter file for {year}: {path}")
    return path


def load_file(path: Path) -> TaxParams:
    """Parse and validate one parameter file (never cached - used by tooling and tests)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxParamsNotFound(f"cannot read tax parameter file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TaxParamsConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaxParamsConfigError(f"{path}: expected a mapping at the top level")
    try:
        return TaxParams.model_validate(raw)
    except ValidationError as exc:
        raise TaxParamsConfigError(f"{path}: {exc}") from exc


@cache
def load(year: int) -> TaxParams:
    """Return the validated parameters for ``year`` (cached; see :func:`clear_cache`)."""
    params = load_file(config_path(year))
    if params.year != year:
        raise TaxParamsConfigError(
            f"{config_path(year)}: document declares year {params.year}, expected {year}"
        )
    return params


def clear_cache() -> None:
    """Forget cached parameter files (call after editing a YAML in a running process)."""
    load.cache_clear()


def iter_config_files(*, config_dir: Path | None = None) -> tuple[Path, ...]:
    """All shipped parameter files, sorted by path."""
    return tuple(sorted((config_dir or CONFIG_DIR).glob("*.yaml")))


def _payload(params: TaxParams) -> dict[str, object]:
    """JSON-safe payload: decimals are stored as exact strings, not floats."""
    document = params.model_dump(mode="json")
    return {key: value for key, value in document.items() if key not in _HEADER_FIELDS}


def upsert(session: Session, params: TaxParams) -> tuple[TaxYearParamsRow, bool]:
    """Insert or update the row for ``(params.year, params.version)``.

    Idempotent by design: re-seeding the same document rewrites that row instead of
    creating a duplicate (``UNIQUE(year, version)`` backs this up). Returns the row and
    whether it was created.
    """
    row = session.scalars(
        select(TaxYearParamsRow).where(
            TaxYearParamsRow.year == params.year,
            TaxYearParamsRow.version == params.version,
        )
    ).one_or_none()
    created = row is None
    if row is None:
        row = TaxYearParamsRow(
            year=params.year,
            version=params.version,
            payload=_payload(params),
            source_url=str(params.source_url),
            effective_from=params.effective_from,
        )
        session.add(row)
    else:
        row.payload = _payload(params)
        row.source_url = str(params.source_url)
        row.effective_from = params.effective_from
    session.flush()
    return row, created


def seed(session: Session, year: int) -> tuple[TaxYearParamsRow, bool]:
    """Load ``year`` from disk and upsert it."""
    return upsert(session, load(year))


def latest(session: Session, year: int) -> TaxYearParamsRow | None:
    """Highest-version stored parameters for ``year`` (``None`` when never seeded)."""
    return session.scalars(
        select(TaxYearParamsRow)
        .where(TaxYearParamsRow.year == year)
        .order_by(TaxYearParamsRow.version.desc())
        .limit(1)
    ).first()


def from_row(row: TaxYearParamsRow) -> TaxParams:
    """Rebuild the validated document from a stored row (columns win over the payload)."""
    document: dict[str, object] = {
        **row.payload,
        "year": row.year,
        "version": row.version,
        "source_url": row.source_url,
        "effective_from": row.effective_from,
    }
    try:
        return TaxParams.model_validate(document)
    except ValidationError as exc:
        raise TaxParamsConfigError(
            f"stored parameters {row.year} v{row.version} are not valid: {exc}"
        ) from exc


def rules_by_kind(params: TaxParams) -> dict[DeadlineKind, list[DeadlineRuleSpec]]:
    """Deadline rules grouped by the kind they generate."""
    grouped: dict[DeadlineKind, list[DeadlineRuleSpec]] = {}
    for spec in params.deadlines:
        grouped.setdefault(spec.kind, []).append(spec)
    return grouped


def rule_names(params: TaxParams) -> Sequence[DeadlineRule]:
    """Rule names declared by ``params``, in file order."""
    return [spec.rule for spec in params.deadlines]
