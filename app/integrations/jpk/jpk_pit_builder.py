"""JPK_PIT builder: revenue/cost documents of a period -> JPK_PIT XML.

The module maps stored :class:`~app.db.models.Document` rows of type
``VAT_INVOICE_SALES`` (revenue, ``PRZYCHOD``) and ``VAT_INVOICE_COST`` (cost, ``KOSZT``)
onto a JPK-style XML statement. Amounts are :class:`~decimal.Decimal` throughout and are
never converted through ``float``; every row carries the document date, the net/VAT/gross
amounts and the counterparty NIP. No name or address is written - the file holds only the
data JPK needs.

**The shape of JPK_PIT is provisional.** At the time of writing the Ministry of Finance
(MF) had not published a verified JPK_PIT XSD that this repository could pin by checksum
(see the ``jpk_pit`` entry of ``config/schemas.yaml``), so the format metadata below
(:data:`NAMESPACE`, :data:`KOD_FORMULARZA`, :data:`WERSJA_SCHEMY`,
:data:`WARIANT_FORMULARZA`) is a local, clearly non-official placeholder. The namespace is
a ``urn:`` and not a ``crd.gov.pl`` address precisely so that nobody mistakes the file for
an official publication. When an official schema is confirmed, replace the constants and
fill in ``url``/``sha256`` in the registry; the structural validator follows the same
constants, so there is a single place to change.

**The generated file is not submitted to MF.** Nothing here talks to any MF system: the
XML is returned for review and manual upload, and the ``Naglowek/SystemInfo`` element says
so inside the document itself.

Structural validation in :func:`validate_jpk_pit` checks required elements, value types
(dates, amounts with at most two fraction digits, NIP checksums), row totals against
``Podsumowanie`` and - when the source documents are supplied - the summary against those
documents. Findings are :class:`~app.integrations.schemas.Violation` objects, which the API
turns into a single ``422`` response.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree as ET

from lxml import etree

from app.db.models import Document
from app.domain.enums import DocumentType
from app.integrations.ksef.fa3_builder import is_valid_nip
from app.integrations.schemas import Violation

__all__ = [
    "CEL_ZLOZENIA",
    "JPK_PIT_KEY",
    "KOD_FORMULARZA",
    "KOD_SYSTEMOWY",
    "NAMESPACE",
    "SYSTEM_INFO",
    "TYP_KOSZT",
    "TYP_PRZYCHOD",
    "WARIANT_FORMULARZA",
    "WERSJA_SCHEMY",
    "JpkPitError",
    "JpkPitRow",
    "JpkPitSummary",
    "JpkPitTotalsMismatch",
    "JpkPitValidationError",
    "build",
    "collect_rows",
    "period_bounds",
    "summarize",
    "validate_jpk_pit",
]

#: Registry key of the JPK_PIT XSD (optional entry, see ``config/schemas.yaml``).
JPK_PIT_KEY: Final[str] = "jpk_pit"

#: Provisional default namespace - *not* an official MF address (see the module docstring).
NAMESPACE: Final[str] = "urn:taxwise:jpk-pit:1-0"

#: Provisional JPK_PIT form metadata; the official values require confirmation at the source.
KOD_FORMULARZA: Final[str] = "JPK_PIT"
KOD_SYSTEMOWY: Final[str] = "JPK_PIT"
WERSJA_SCHEMY: Final[str] = "1-0"
WARIANT_FORMULARZA: Final[int] = 1

#: ``CelZlozenia``: 1 = the original statement, 2 = a correction (JPK convention).
CEL_ZLOZENIA: Final[str] = "1"

#: Row types of the statement.
TYP_PRZYCHOD: Final[str] = "PRZYCHOD"
TYP_KOSZT: Final[str] = "KOSZT"

#: Mapping of stored document types onto JPK_PIT row types.
ROW_TYPES: Final[dict[DocumentType, str]] = {
    DocumentType.VAT_INVOICE_SALES: TYP_PRZYCHOD,
    DocumentType.VAT_INVOICE_COST: TYP_KOSZT,
}

#: Marker written to ``Naglowek/SystemInfo`` - an explicit "not transmitted" statement.
SYSTEM_INFO: Final[str] = (
    "TaxWise SME - plik wygenerowany lokalnie, NIE został przesłany do systemu MF"
)

_CENT: Final[Decimal] = Decimal("0.01")
_ZERO: Final[Decimal] = Decimal("0.00")
_STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"

#: Amounts are written with a dot and at most two fraction digits (no exponent notation).
_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(r"-?\d+(?:\.\d{1,2})?")

#: ``CelZlozenia`` values accepted by the validator (original / correction).
_CEL_VALUES: Final[frozenset[str]] = frozenset({"1", "2"})


class JpkPitError(ValueError):
    """Base class for JPK_PIT generation problems; carries the violations found."""

    violations: tuple[Violation, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """JSON-ready error payload (``message`` + ``violations``)."""
        return {
            "message": str(self),
            "violations": [violation.as_dict() for violation in self.violations],
        }


class JpkPitValidationError(JpkPitError):
    """Input documents are missing data or are inconsistent; ``violations`` lists them."""

    def __init__(self, violations: Sequence[Violation], message: str | None = None) -> None:
        self.violations = tuple(violations)
        detail = "; ".join(f"{item.path}: {item.message}" for item in self.violations)
        super().__init__(message or f"JPK_PIT data is not valid: {detail}")


class JpkPitTotalsMismatch(JpkPitValidationError):
    """A document's net + VAT does not add up to its stored gross amount."""


def _money(value: Decimal) -> Decimal:
    """Round to grosze with ``ROUND_HALF_UP`` (the convention used by the calc engine)."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _money_text(value: Decimal) -> str:
    return f"{_money(value):.2f}"


def _stamp(value: datetime) -> str:
    """``DataWytworzenia``: UTC, second precision, trailing ``Z`` (``xs:dateTime``)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).strftime(_STAMP_FORMAT)


def period_bounds(year: int, month: int | None) -> tuple[date, date]:
    """First and last day reported by the statement.

    ``month=None`` produces an annual statement (1 January - 31 December), which is what
    the tax pack uses; a month produces that month's bounds.
    """
    if not 1 <= year <= 9999:
        raise JpkPitError(f"year must be between 1 and 9999, got {year}")
    if month is None:
        return date(year, 1, 1), date(year, 12, 31)
    if not 1 <= month <= 12:
        raise JpkPitError(f"month must be between 1 and 12, got {month}")
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


@dataclass(frozen=True, slots=True)
class JpkPitRow:
    """One ``Pozycja`` of the statement, derived from a single stored document."""

    typ: str
    data: date
    netto: Decimal
    vat: Decimal
    brutto: Decimal
    nip_kontrahenta: str


@dataclass(frozen=True, slots=True)
class JpkPitSummary:
    """Aggregates written to ``Podsumowanie`` and compared with the rows on validation."""

    liczba_przychodow: int
    liczba_kosztow: int
    przychody_netto: Decimal
    przychody_vat: Decimal
    przychody_brutto: Decimal
    koszty_netto: Decimal
    koszty_vat: Decimal
    koszty_brutto: Decimal

    @property
    def saldo_brutto(self) -> Decimal:
        """Revenue gross minus cost gross (the statement's gross balance)."""
        return _money(self.przychody_brutto - self.koszty_brutto)


def collect_rows(documents: Sequence[Document]) -> tuple[JpkPitRow, ...]:
    """Map documents onto statement rows, raising :class:`JpkPitError` on any finding.

    Rows are ordered by issue date and then id, which makes the output deterministic.
    A revenue/cost document must carry a PLN currency, a net and a VAT amount, a gross
    amount equal to ``net + VAT`` and a checksum-valid counterparty NIP; anything else is
    reported instead of guessed.
    """
    rows: list[JpkPitRow] = []
    violations: list[Violation] = []
    totals_mismatch = False

    for document in sorted(documents, key=lambda item: (item.issue_date, item.id or 0)):
        where = f"document[{document.id}]" if document.id is not None else "document"
        typ = ROW_TYPES.get(document.type)
        if typ is None:
            violations.append(
                Violation(
                    f"{where}/type",
                    "JPK_PIT accepts only VAT_INVOICE_SALES and VAT_INVOICE_COST, "
                    f"got {document.type}",
                )
            )
            continue
        if (document.currency or "").upper() != "PLN":
            violations.append(
                Violation(
                    f"{where}/currency",
                    f"JPK_PIT amounts are reported in PLN, got {document.currency!r}",
                )
            )

        netto = document.amount_net
        vat = document.vat_amount
        brutto = document.amount_gross
        for label, value in (
            ("amount_net", netto),
            ("vat_amount", vat),
            ("amount_gross", brutto),
        ):
            if value is None:
                violations.append(
                    Violation(
                        f"{where}/{label}",
                        f"the document has no {label}; the JPK_PIT row cannot be built",
                    )
                )
        if netto is None or vat is None or brutto is None:
            continue

        if _money(netto + vat) != _money(brutto):
            totals_mismatch = True
            violations.append(
                Violation(
                    f"{where}/amount_gross",
                    f"document gross is {_money_text(brutto)} but net + VAT is "
                    f"{_money_text(_money(netto + vat))}",
                )
            )

        nip = (document.counterparty_nip or "").strip()
        if not nip:
            violations.append(
                Violation(
                    f"{where}/counterparty_nip",
                    "a JPK_PIT row requires the counterparty NIP",
                )
            )
        elif not is_valid_nip(nip):
            violations.append(
                Violation(
                    f"{where}/counterparty_nip",
                    f"{nip!r} is not a checksum-valid Polish NIP",
                )
            )

        rows.append(
            JpkPitRow(
                typ=typ,
                data=document.issue_date,
                netto=_money(netto),
                vat=_money(vat),
                brutto=_money(brutto),
                nip_kontrahenta=nip,
            )
        )

    if violations:
        error = JpkPitTotalsMismatch if totals_mismatch else JpkPitValidationError
        raise error(violations)
    return tuple(rows)


def summarize(rows: Sequence[JpkPitRow]) -> JpkPitSummary:
    """Aggregate rows into the ``Podsumowanie`` block (always rounded to grosze)."""
    revenue = [row for row in rows if row.typ == TYP_PRZYCHOD]
    cost = [row for row in rows if row.typ == TYP_KOSZT]

    def total(items: Sequence[JpkPitRow], attribute: str) -> Decimal:
        return _money(sum((getattr(row, attribute) for row in items), _ZERO))

    return JpkPitSummary(
        liczba_przychodow=len(revenue),
        liczba_kosztow=len(cost),
        przychody_netto=total(revenue, "netto"),
        przychody_vat=total(revenue, "vat"),
        przychody_brutto=total(revenue, "brutto"),
        koszty_netto=total(cost, "netto"),
        koszty_vat=total(cost, "vat"),
        koszty_brutto=total(cost, "brutto"),
    )


def _q(name: str) -> str:
    """Qualified element name in the JPK_PIT default namespace."""
    return f"{{{NAMESPACE}}}{name}"


def _add(
    parent: ET.Element,
    name: str,
    text: str | None = None,
    **attributes: str,
) -> ET.Element:
    element = ET.SubElement(parent, _q(name))
    if text is not None:
        element.text = text
    for key, value in attributes.items():
        element.set(key, value)
    return element


def _render(
    rows: Sequence[JpkPitRow],
    summary: JpkPitSummary,
    *,
    data_od: date,
    data_do: date,
    created_at: datetime,
) -> bytes:
    """Serialise the statement; input has already been validated by :func:`collect_rows`."""
    ET.register_namespace("", NAMESPACE)
    root = ET.Element(_q("JPK_PIT"))

    header = _add(root, "Naglowek")
    _add(
        header,
        "KodFormularza",
        KOD_FORMULARZA,
        kodSystemowy=KOD_SYSTEMOWY,
        wersjaSchemy=WERSJA_SCHEMY,
    )
    _add(header, "WariantFormularza", str(WARIANT_FORMULARZA))
    _add(header, "DataOd", data_od.isoformat())
    _add(header, "DataDo", data_do.isoformat())
    _add(header, "CelZlozenia", CEL_ZLOZENIA)
    _add(header, "DataWytworzenia", _stamp(created_at))
    _add(header, "SystemInfo", SYSTEM_INFO)

    positions = _add(root, "Pozycje")
    for row in rows:
        position = _add(positions, "Pozycja")
        _add(position, "Typ", row.typ)
        _add(position, "Data", row.data.isoformat())
        _add(position, "KwotaNetto", _money_text(row.netto))
        _add(position, "KwotaVat", _money_text(row.vat))
        _add(position, "KwotaBrutto", _money_text(row.brutto))
        _add(position, "NipKontrahenta", row.nip_kontrahenta)

    totals = _add(root, "Podsumowanie")
    _add(totals, "LiczbaPrzychodow", str(summary.liczba_przychodow))
    _add(totals, "LiczbaKosztow", str(summary.liczba_kosztow))
    _add(totals, "SumaPrzychodowNetto", _money_text(summary.przychody_netto))
    _add(totals, "SumaPrzychodowVat", _money_text(summary.przychody_vat))
    _add(totals, "SumaPrzychodowBrutto", _money_text(summary.przychody_brutto))
    _add(totals, "SumaKosztowNetto", _money_text(summary.koszty_netto))
    _add(totals, "SumaKosztowVat", _money_text(summary.koszty_vat))
    _add(totals, "SumaKosztowBrutto", _money_text(summary.koszty_brutto))
    _add(totals, "SaldoBrutto", _money_text(summary.saldo_brutto))

    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    ).encode("utf-8")


def build(
    documents: Sequence[Document],
    year: int,
    month: int | None,
    *,
    created_at: datetime | None = None,
) -> bytes:
    """Build the JPK_PIT XML for the documents of ``year``/``month`` (``None`` = year).

    Raises :class:`JpkPitError` when a document cannot be mapped; an empty period is a
    valid statement whose ``Podsumowanie`` carries the count zero and ``0.00`` amounts.
    The returned bytes are never submitted to MF - see :data:`SYSTEM_INFO`.
    """
    data_od, data_do = period_bounds(year, month)
    rows = collect_rows(documents)
    return _render(
        rows,
        summarize(rows),
        data_od=data_od,
        data_do=data_do,
        created_at=created_at or datetime.now(UTC),
    )


def _parse_decimal(text: str | None) -> Decimal | None:
    """Parse a plain decimal with at most two fraction digits; reject exponent notation."""
    if text is None:
        return None
    stripped = text.strip()
    if not _AMOUNT_RE.fullmatch(stripped):
        return None
    try:
        value = Decimal(stripped)
    except InvalidOperation:  # pragma: no cover - the regex already excludes invalid input
        return None
    return value if value.is_finite() else None


def _parse_date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


class _Checker:
    """Walks one JPK_PIT document and accumulates :class:`Violation` objects."""

    def __init__(self) -> None:
        self.violations: list[Violation] = []

    def add(self, path: str, message: str) -> None:
        self.violations.append(Violation(path=path, message=message))

    def find(self, parent: etree._Element, name: str) -> etree._Element | None:
        return parent.find(_q(name))

    def text(self, parent: etree._Element, name: str) -> str | None:
        element = self.find(parent, name)
        if element is None or element.text is None:
            return None
        return str(element.text).strip()

    def require(self, parent: etree._Element, name: str, path: str) -> str | None:
        value = self.text(parent, name)
        if not value:
            self.add(f"{path}/{name}", "required element is missing or empty")
            return None
        return value

    def decimal(self, parent: etree._Element, name: str, path: str) -> Decimal | None:
        value = self.require(parent, name, path)
        if value is None:
            return None
        parsed = _parse_decimal(value)
        if parsed is None:
            self.add(
                f"{path}/{name}",
                f"{value!r} is not a decimal amount with at most two fraction digits",
            )
        return parsed

    def integer(self, parent: etree._Element, name: str, path: str) -> int | None:
        value = self.require(parent, name, path)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            self.add(f"{path}/{name}", f"{value!r} is not an integer")
            return None

    def header(self, root: etree._Element) -> None:
        header = self.find(root, "Naglowek")
        if header is None:
            self.add("JPK_PIT/Naglowek", "required element is missing")
            return

        kod = self.find(header, "KodFormularza")
        if kod is None or not (kod.text or "").strip():
            self.add("Naglowek/KodFormularza", "required element is missing or empty")
        else:
            if (kod.text or "").strip() != KOD_FORMULARZA:
                self.add(
                    "Naglowek/KodFormularza",
                    f"{kod.text!r} is not {KOD_FORMULARZA!r}",
                )
            if kod.get("kodSystemowy") != KOD_SYSTEMOWY:
                self.add(
                    "Naglowek/KodFormularza/@kodSystemowy",
                    f"expected {KOD_SYSTEMOWY!r}, got {kod.get('kodSystemowy')!r}",
                )
            if kod.get("wersjaSchemy") != WERSJA_SCHEMY:
                self.add(
                    "Naglowek/KodFormularza/@wersjaSchemy",
                    f"expected {WERSJA_SCHEMY!r}, got {kod.get('wersjaSchemy')!r}",
                )

        wariant = self.integer(header, "WariantFormularza", "Naglowek")
        if wariant is not None and wariant <= 0:
            self.add("Naglowek/WariantFormularza", "wariant must be a positive integer")

        data_od = _parse_date(self.require(header, "DataOd", "Naglowek"))
        if self.text(header, "DataOd") is not None and data_od is None:
            self.add("Naglowek/DataOd", "DataOd is not an ISO-8601 date")
        data_do = _parse_date(self.require(header, "DataDo", "Naglowek"))
        if self.text(header, "DataDo") is not None and data_do is None:
            self.add("Naglowek/DataDo", "DataDo is not an ISO-8601 date")
        if data_od is not None and data_do is not None and data_od > data_do:
            self.add("Naglowek/DataOd", "DataOd must not be after DataDo")

        cel = self.require(header, "CelZlozenia", "Naglowek")
        if cel is not None and cel not in _CEL_VALUES:
            self.add(
                "Naglowek/CelZlozenia",
                f"{cel!r} is not a known purpose of submission (1 or 2)",
            )

    def positions(self, root: etree._Element) -> list[JpkPitRow]:
        container = self.find(root, "Pozycje")
        if container is None:
            self.add(
                "JPK_PIT/Pozycje",
                "required element is missing (write an empty element when there are no rows)",
            )
            return []
        rows: list[JpkPitRow] = []
        for index, position in enumerate(container.findall(_q("Pozycja")), start=1):
            path = f"Pozycje/Pozycja[{index}]"
            typ = self.require(position, "Typ", path)
            if typ is not None and typ not in (TYP_PRZYCHOD, TYP_KOSZT):
                self.add(f"{path}/Typ", f"{typ!r} is not PRZYCHOD or KOSZT")
                typ = None

            data_text = self.require(position, "Data", path)
            data = _parse_date(data_text)
            if data_text is not None and data is None:
                self.add(f"{path}/Data", f"{data_text!r} is not an ISO-8601 date")

            netto = self.decimal(position, "KwotaNetto", path)
            vat = self.decimal(position, "KwotaVat", path)
            brutto = self.decimal(position, "KwotaBrutto", path)
            if (
                netto is not None
                and vat is not None
                and brutto is not None
                and _money(netto + vat) != _money(brutto)
            ):
                self.add(
                    f"{path}/KwotaBrutto",
                    f"{_money_text(brutto)} does not equal net + VAT "
                    f"({_money_text(_money(netto + vat))})",
                )

            nip = self.require(position, "NipKontrahenta", path)
            if nip is not None and not is_valid_nip(nip):
                self.add(
                    f"{path}/NipKontrahenta",
                    f"{nip!r} is not a checksum-valid Polish NIP",
                )
                nip = None

            if None in (typ, data, netto, vat, brutto, nip):
                continue
            assert typ is not None and data is not None and nip is not None
            assert netto is not None and vat is not None and brutto is not None
            rows.append(
                JpkPitRow(
                    typ=typ,
                    data=data,
                    netto=netto,
                    vat=vat,
                    brutto=brutto,
                    nip_kontrahenta=nip,
                )
            )
        return rows

    def _match_money(
        self, block: etree._Element, name: str, expected: Decimal, source: str
    ) -> None:
        actual = self.decimal(block, name, "Podsumowanie")
        if actual is not None and _money(actual) != _money(expected):
            self.add(
                f"Podsumowanie/{name}",
                f"{_money_text(actual)} does not match {source} ({_money_text(expected)})",
            )

    def _match_count(self, block: etree._Element, name: str, expected: int, source: str) -> None:
        actual = self.integer(block, name, "Podsumowanie")
        if actual is not None and actual != expected:
            self.add(
                f"Podsumowanie/{name}",
                f"{actual} does not match {source} ({expected})",
            )

    def _compare_summary(
        self, block: etree._Element, expected: JpkPitSummary, source: str
    ) -> None:
        self._match_count(block, "LiczbaPrzychodow", expected.liczba_przychodow, source)
        self._match_count(block, "LiczbaKosztow", expected.liczba_kosztow, source)
        self._match_money(block, "SumaPrzychodowNetto", expected.przychody_netto, source)
        self._match_money(block, "SumaPrzychodowVat", expected.przychody_vat, source)
        self._match_money(block, "SumaPrzychodowBrutto", expected.przychody_brutto, source)
        self._match_money(block, "SumaKosztowNetto", expected.koszty_netto, source)
        self._match_money(block, "SumaKosztowVat", expected.koszty_vat, source)
        self._match_money(block, "SumaKosztowBrutto", expected.koszty_brutto, source)
        self._match_money(block, "SaldoBrutto", expected.saldo_brutto, source)

    def summary(
        self,
        root: etree._Element,
        rows: Sequence[JpkPitRow],
        documents: Sequence[Document] | None,
    ) -> None:
        block = self.find(root, "Podsumowanie")
        if block is None:
            self.add("JPK_PIT/Podsumowanie", "required element is missing")
            return
        self._compare_summary(block, summarize(rows), "the rows")
        if documents is None:
            return
        try:
            from_documents = summarize(collect_rows(documents))
        except JpkPitError as exc:
            self.violations.extend(exc.violations)
            return
        self._compare_summary(block, from_documents, "the source documents")

    def document(self, root: etree._Element, documents: Sequence[Document] | None) -> None:
        self.header(root)
        rows = self.positions(root)
        self.summary(root, rows, documents)


def validate_jpk_pit(
    xml_bytes: bytes,
    *,
    documents: Sequence[Document] | None = None,
) -> list[Violation]:
    """Structurally validate a JPK_PIT document; returns every finding (empty = valid).

    ``documents`` is optional: when supplied, the ``Podsumowanie`` block is additionally
    compared with the aggregates of those source documents, so a statement can never
    silently disagree with the rows it was generated from.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        return [Violation(path="document", message=f"not well-formed XML: {exc}")]

    if root.tag != _q("JPK_PIT"):
        return [
            Violation(
                path="JPK_PIT",
                message=f"root element must be {_q('JPK_PIT')}, got {root.tag!r}",
            )
        ]

    checker = _Checker()
    checker.document(root, documents)
    return checker.violations
