"""FA(3) invoice XML builder (KSeF logical structure).

The module turns a stored :class:`~app.db.models.Document` of type ``VAT_INVOICE_SALES``
plus the invoice details (seller, buyer, line items) into a byte-exact FA(3) XML document.
Amounts are :class:`~decimal.Decimal` throughout; the net/VAT/gross totals are computed
from the line items *per VAT rate group* and compared with the amounts stored on the
document - any difference is reported as a validation violation, never silently rounded.

The format metadata (default namespace, ``KodFormularza`` attributes, ``WariantFormularza``
and the human-readable schema version) comes from ``config/schemas.yaml`` through
:func:`app.integrations.schemas.fa3_format`; this module keeps no schema version literal.

**The generated file is not sent to KSeF.** Nothing here talks to the KSeF API: the XML
is returned to the caller (and carries a ``SystemInfo`` marker saying so) for review,
signing and upload by an authorised tool.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final
from xml.etree import ElementTree as ET

from app.db.models import Document
from app.domain.enums import DocumentType
from app.integrations import schemas
from app.integrations.schemas import Violation

__all__ = [
    "RATE_SLOTS",
    "SYSTEM_INFO",
    "Fa3Error",
    "Fa3Invoice",
    "Fa3Totals",
    "Fa3TotalsMismatch",
    "Fa3ValidationError",
    "LineItem",
    "Party",
    "RateSlot",
    "RateTotal",
    "VatRate",
    "build",
    "is_valid_nip",
]

#: Marker written to ``Naglowek/SystemInfo`` - an explicit "not transmitted" statement.
SYSTEM_INFO: Final[str] = (
    "TaxWise SME - plik wygenerowany lokalnie, NIE został przesłany do KSeF"
)

_CENT: Final[Decimal] = Decimal("0.01")
_ZERO: Final[Decimal] = Decimal("0.00")
_STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"

#: NIP checksum weights (the tenth digit is the control digit).
_NIP_WEIGHTS: Final[tuple[int, ...]] = (6, 5, 7, 2, 3, 4, 5, 6, 7)
_NIP_INPUT_RE: Final[re.Pattern[str]] = re.compile(r"\D")
_COUNTRY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z]{2}")
_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z]{3}")


class Fa3Error(ValueError):
    """Base class for FA(3) generation problems; carries the violations found."""

    violations: tuple[Violation, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """JSON-ready error payload (``message`` + ``violations``)."""
        return {
            "message": str(self),
            "violations": [violation.as_dict() for violation in self.violations],
        }


class Fa3ValidationError(Fa3Error):
    """Input data is missing or inconsistent; ``violations`` lists every finding."""

    def __init__(self, violations: Sequence[Violation], message: str | None = None) -> None:
        self.violations = tuple(violations)
        detail = "; ".join(f"{item.path}: {item.message}" for item in self.violations)
        super().__init__(message or f"FA(3) invoice data is not valid: {detail}")


class Fa3TotalsMismatch(Fa3ValidationError):
    """The line items do not add up to the net/VAT/gross amounts stored on the document."""


def is_valid_nip(value: str) -> bool:
    """Whether ``value`` (digits, separators allowed) is a checksum-valid Polish NIP."""
    digits = _NIP_INPUT_RE.sub("", value)
    if len(digits) != 10 or not digits.isdigit():
        return False
    checksum = sum(
        weight * int(digit) for weight, digit in zip(_NIP_WEIGHTS, digits[:9], strict=True)
    )
    control = checksum % 11
    return control != 10 and control == int(digits[9])


class VatRate(StrEnum):
    """FA(3) ``P_12`` codes supported by this builder (23/8/5/0 percent and exemption)."""

    R23 = "23"
    R8 = "8"
    R5 = "5"
    R0 = "0"
    ZW = "zw"

    @property
    def percent(self) -> Decimal | None:
        """The rate as a percentage, or ``None`` for the exempt (``zw``) code."""
        return None if self is VatRate.ZW else Decimal(self.value)


@dataclass(frozen=True, slots=True)
class RateSlot:
    """Where a rate's summary belongs in ``Fa`` (``P_13_x`` / ``P_14_x`` / ``P_12``)."""

    p12: str
    net_element: str
    vat_element: str | None


#: The FA(3) mapping of the supported rates. ``P_12`` is a code, not a fraction: the
#: zero-rate code is the domestic one (``0 KR``) and exemption carries no VAT element.
RATE_SLOTS: Final[Mapping[VatRate, RateSlot]] = {
    VatRate.R23: RateSlot("23", "P_13_1", "P_14_1"),
    VatRate.R8: RateSlot("8", "P_13_2", "P_14_2"),
    VatRate.R5: RateSlot("5", "P_13_3", "P_14_3"),
    VatRate.R0: RateSlot("0 KR", "P_13_4", "P_14_4"),
    VatRate.ZW: RateSlot("zw", "P_13_7", None),
}


def _decimal(value: object, field: str) -> Decimal:
    """Convert ``value`` to a finite ``Decimal``; floats go through their string form."""
    if isinstance(value, bool) or value is None:
        raise Fa3Error(f"{field} must be a decimal number, got {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise Fa3Error(f"{field} is not a decimal number: {value!r}") from exc
    else:
        raise Fa3Error(f"{field} must be a decimal number, got {value!r}")
    if not result.is_finite():
        raise Fa3Error(f"{field} must be a finite decimal number, got {value!r}")
    return result


def _money(value: Decimal) -> Decimal:
    """Round to grosze with ``ROUND_HALF_UP`` (the convention used by the calc engine)."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _money_text(value: Decimal) -> str:
    return f"{_money(value):.2f}"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Fa3Error(f"{field} is required")
    return value.strip()


def _coerce_rate(value: object) -> VatRate:
    """Accept a ``VatRate``, a ``P_12`` code (``"23"``/``"zw"``) or a decimal fraction."""
    if isinstance(value, VatRate):
        return value
    if isinstance(value, str):
        normalised = value.strip().lower().replace("%", "").replace("0 kr", "0")
        aliases = {"zw.": "zw", "exempt": "zw", "np": "zw"}
        normalised = aliases.get(normalised, normalised)
        for rate in VatRate:
            if normalised == rate.value:
                return rate
        raise Fa3Error(f"unsupported VAT rate {value!r}; use 23, 8, 5, 0 or zw")
    fraction = _decimal(value, "vat rate")
    percent = fraction * 100 if fraction <= 1 else fraction
    for rate in VatRate:
        if rate is not VatRate.ZW and percent == Decimal(rate.value):
            return rate
    raise Fa3Error(f"unsupported VAT rate {value!r}; use 23, 8, 5, 0 or zw")


@dataclass(frozen=True, slots=True)
class Party:
    """A seller (``Podmiot1``) or buyer (``Podmiot2``) identity.

    ``jst``/``gv`` are the FA(3) yes/no flags ("1" = yes, "2" = no) for a public-entity
    and VAT-group member respectively; a sole proprietor selling B2B is ``2``/``2``.
    """

    nip: str
    name: str
    address_line_1: str
    address_line_2: str | None = None
    country_code: str = "PL"
    jst: str = "2"
    gv: str = "2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "nip", _NIP_INPUT_RE.sub("", self.nip))
        object.__setattr__(self, "country_code", self.country_code.strip().upper())
        object.__setattr__(self, "jst", str(self.jst).strip())
        object.__setattr__(self, "gv", str(self.gv).strip())


@dataclass(frozen=True, slots=True)
class LineItem:
    """One ``FaWiersz``: quantity x unit net price at a single VAT rate.

    ``vat_rate`` accepts the ``P_12`` code (``"23"``, ``"8"``, ``"5"``, ``"0"``, ``"zw"``)
    or the same rate as a decimal fraction (``Decimal("23") / 100``), so callers can reuse
    the convention of the calculation API. ``net`` is rounded to grosze once, on the line.
    """

    description: str
    quantity: Decimal
    unit: str
    unit_net_price: Decimal
    vat_rate: VatRate | str | Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _decimal(self.quantity, "line quantity"))
        object.__setattr__(self, "unit_net_price", _decimal(self.unit_net_price, "unit net price"))
        object.__setattr__(self, "vat_rate", _coerce_rate(self.vat_rate))

    @property
    def rate(self) -> VatRate:
        """Rate of the line as the canonical enum (``vat_rate`` may be a raw value)."""
        return _coerce_rate(self.vat_rate)

    @property
    def net(self) -> Decimal:
        """Line net value: ``quantity * unit_net_price`` rounded to grosze."""
        return _money(self.quantity * self.unit_net_price)


@dataclass(frozen=True, slots=True)
class RateTotal:
    """Net and VAT for one rate group, as written to ``P_13_x`` / ``P_14_x``."""

    rate: VatRate
    net: Decimal
    vat: Decimal


@dataclass(frozen=True, slots=True)
class Fa3Totals:
    """Invoice totals: the sum of the rate groups, which must match the document amounts."""

    net: Decimal
    vat: Decimal
    gross: Decimal
    by_rate: tuple[RateTotal, ...]

    @property
    def has_exempt_sales(self) -> bool:
        """Whether any line was invoiced at the exempt (``zw``) rate."""
        return any(total.rate is VatRate.ZW for total in self.by_rate)


@dataclass(frozen=True, slots=True)
class Fa3Invoice:
    """Invoice data that is not stored on the ``documents`` row.

    The issue date comes from the document; ``sale_date`` (``P_6``) and ``place`` (``P_1M``)
    are optional. ``exemption_basis`` is the legal basis written to ``P_19`` and is
    mandatory as soon as one line is invoiced ``zw``.
    """

    number: str
    seller: Party
    buyer: Party
    lines: tuple[LineItem, ...]
    sale_date: date | None = None
    place: str | None = None
    currency: str = "PLN"
    exemption_basis: str | None = None

    def totals(self) -> Fa3Totals:
        """Aggregate the lines per rate group (rate order is the FA(3) element order).

        VAT is computed *per rate group*, which is exactly what ``P_14_x`` means ("kwota
        podatku od sumy wartości sprzedaży netto objętej stawką"): the group net is rounded
        to grosze first, then taxed and rounded again.
        """
        nets: dict[VatRate, Decimal] = {}
        for line in self.lines:
            rate = line.rate
            nets[rate] = nets.get(rate, _ZERO) + line.net
        by_rate: list[RateTotal] = []
        for rate in VatRate:
            if rate not in nets:
                continue
            net = _money(nets[rate])
            percent = rate.percent
            vat = _ZERO if percent is None else _money(net * percent / 100)
            by_rate.append(RateTotal(rate=rate, net=net, vat=vat))
        net_total = _money(sum((total.net for total in by_rate), _ZERO))
        vat_total = _money(sum((total.vat for total in by_rate), _ZERO))
        return Fa3Totals(
            net=net_total,
            vat=vat_total,
            gross=_money(net_total + vat_total),
            by_rate=tuple(by_rate),
        )



def _validate_party(party: Party, path: str, violations: list[Violation]) -> None:
    """Collect required-element/type findings for one party block."""
    ident = f"{path}/DaneIdentyfikacyjne"
    if not party.nip:
        violations.append(Violation(f"{ident}/NIP", "NIP is required"))
    elif not is_valid_nip(party.nip):
        violations.append(
            Violation(f"{ident}/NIP", f"{party.nip!r} is not a checksum-valid Polish NIP")
        )
    if not party.name.strip():
        violations.append(Violation(f"{ident}/Nazwa", "name is required"))
    if not party.address_line_1.strip():
        violations.append(Violation(f"{path}/Adres/AdresL1", "address line 1 is required"))
    if not _COUNTRY_RE.fullmatch(party.country_code):
        violations.append(
            Violation(f"{path}/Adres/KodKraju", "country code must be two letters, e.g. PL")
        )
    if party.jst not in {"1", "2"}:
        violations.append(Violation(f"{path}/JST", 'JST must be "1" or "2"'))
    if party.gv not in {"1", "2"}:
        violations.append(Violation(f"{path}/GV", 'GV must be "1" or "2"'))


def _amount(value: Decimal | None, path: str, label: str, violations: list[Violation]) -> None:
    if value is None:
        violations.append(
            Violation(path, f"the document has no {label}; FA(3) totals cannot be checked")
        )
    elif value < _ZERO:
        violations.append(Violation(path, f"the document {label} is negative"))


def _total_findings(
    document: Document, totals: Fa3Totals, violations: list[Violation]
) -> bool:
    """Compare computed totals with the stored document amounts; return mismatch flag."""
    _amount(document.amount_net, "document/amount_net", "net amount", violations)
    _amount(document.vat_amount, "document/vat_amount", "VAT amount", violations)
    mismatched = False
    pairs = (
        ("document/amount_net", "net", document.amount_net, totals.net, "P_13_1..P_13_7"),
        ("document/vat_amount", "VAT", document.vat_amount, totals.vat, "P_14_1..P_14_4"),
        ("document/amount_gross", "gross", document.amount_gross, totals.gross, "P_15"),
    )
    for path, label, expected, computed, elements in pairs:
        if expected is None:
            continue
        if _money(expected) != computed:
            mismatched = True
            violations.append(
                Violation(
                    path,
                    f"document {label} is {_money_text(expected)} but the lines add up to "
                    f"{_money_text(computed)} ({elements})",
                )
            )
    return mismatched


def _collect(
    document: Document, invoice: Fa3Invoice, totals: Fa3Totals
) -> tuple[list[Violation], bool]:
    """Every input finding plus whether the totals disagree with the document."""
    violations: list[Violation] = []
    if document.type is not DocumentType.VAT_INVOICE_SALES:
        violations.append(
            Violation(
                "document/type",
                "FA(3) sales invoices can only be built for VAT_INVOICE_SALES, "
                f"got {document.type}",
            )
        )
    if not isinstance(invoice.currency, str) or not _CURRENCY_RE.fullmatch(invoice.currency):
        violations.append(Violation("Fa/KodWaluty", "currency must be a three-letter code"))
    if not document.currency:
        violations.append(Violation("document/currency", "the document has no currency"))
    elif invoice.currency != document.currency.upper():
        violations.append(
            Violation(
                "Fa/KodWaluty",
                f"invoice currency {invoice.currency!r} does not match the document "
                f"currency {document.currency!r}",
            )
        )

    number = invoice.number if isinstance(invoice.number, str) else ""
    if not number.strip():
        violations.append(Violation("Fa/P_2", "invoice number is required"))
    elif len(number.strip()) > 256:
        violations.append(Violation("Fa/P_2", "invoice number must not exceed 256 characters"))
    if invoice.place is not None and not invoice.place.strip():
        violations.append(Violation("Fa/P_1M", "place of issue must not be blank"))

    _validate_party(invoice.seller, "Podmiot1", violations)
    _validate_party(invoice.buyer, "Podmiot2", violations)
    if (
        document.counterparty_nip
        and invoice.buyer.nip
        and document.counterparty_nip != invoice.buyer.nip
    ):
        violations.append(
            Violation(
                "Podmiot2/DaneIdentyfikacyjne/NIP",
                f"buyer NIP {invoice.buyer.nip} does not match the document "
                f"counterparty {document.counterparty_nip}",
            )
        )

    if not invoice.lines:
        violations.append(Violation("Fa/FaWiersz", "at least one line item is required"))
    elif len(invoice.lines) > 10_000:
        violations.append(Violation("Fa/FaWiersz", "at most 10000 line items are allowed"))
    for index, item in enumerate(invoice.lines, start=1):
        where = f"Fa/FaWiersz[{index}]"
        if not isinstance(item.description, str) or not item.description.strip():
            violations.append(Violation(f"{where}/P_7", "description is required"))
        elif len(item.description.strip()) > 512:
            violations.append(
                Violation(f"{where}/P_7", "description must not exceed 512 characters")
            )
        if item.quantity <= _ZERO:
            violations.append(Violation(f"{where}/P_8B", "quantity must be greater than zero"))
        if not isinstance(item.unit, str) or not item.unit.strip():
            violations.append(Violation(f"{where}/P_8A", "unit of measure is required"))
        if item.unit_net_price < _ZERO:
            violations.append(Violation(f"{where}/P_9A", "unit net price must not be negative"))

    if totals.has_exempt_sales and not (invoice.exemption_basis or "").strip():
        violations.append(
            Violation(
                "Fa/Adnotacje/Zwolnienie/P_19",
                "exemption_basis is required when a line uses the zw rate",
            )
        )

    mismatched = _total_findings(document, totals, violations)
    return violations, mismatched



def _stamp(value: datetime) -> str:
    """``DataWytworzeniaFa``: UTC, second precision, trailing ``Z`` (``xs:dateTime``)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).strftime(_STAMP_FORMAT)


def _q(fmt: schemas.Fa3Format, name: str) -> str:
    """Qualified element name in the FA(3) default namespace."""
    return f"{{{fmt.namespace}}}{name}"


def _add(
    parent: ET.Element,
    fmt: schemas.Fa3Format,
    name: str,
    text: str | None = None,
    **attributes: str,
) -> ET.Element:
    element = ET.SubElement(parent, _q(fmt, name))
    if text is not None:
        element.text = text
    for key, value in attributes.items():
        element.set(key, value)
    return element


def _party(parent: ET.Element, fmt: schemas.Fa3Format, name: str, party: Party) -> ET.Element:
    block = _add(parent, fmt, name)
    ident = _add(block, fmt, "DaneIdentyfikacyjne")
    _add(ident, fmt, "NIP", party.nip)
    _add(ident, fmt, "Nazwa", party.name.strip())
    address = _add(block, fmt, "Adres")
    _add(address, fmt, "KodKraju", party.country_code)
    _add(address, fmt, "AdresL1", party.address_line_1.strip())
    if party.address_line_2 and party.address_line_2.strip():
        _add(address, fmt, "AdresL2", party.address_line_2.strip())
    return block


def _render(
    document: Document,
    invoice: Fa3Invoice,
    totals: Fa3Totals,
    fmt: schemas.Fa3Format,
    created_at: datetime,
) -> bytes:
    """Serialise the invoice; input has already been validated by :func:`_collect`."""
    ET.register_namespace("", fmt.namespace)
    root = ET.Element(_q(fmt, "Faktura"))

    header = _add(root, fmt, "Naglowek")
    _add(
        header,
        fmt,
        "KodFormularza",
        fmt.kod_formularza,
        kodSystemowy=fmt.kod_systemowy,
        wersjaSchemy=fmt.wersja_schemy,
    )
    _add(header, fmt, "WariantFormularza", str(fmt.wariant_formularza))
    _add(header, fmt, "DataWytworzeniaFa", _stamp(created_at))
    _add(header, fmt, "SystemInfo", SYSTEM_INFO)

    _party(root, fmt, "Podmiot1", invoice.seller)
    buyer = _party(root, fmt, "Podmiot2", invoice.buyer)
    _add(buyer, fmt, "JST", invoice.buyer.jst)
    _add(buyer, fmt, "GV", invoice.buyer.gv)

    body = _add(root, fmt, "Fa")
    _add(body, fmt, "KodWaluty", invoice.currency)
    _add(body, fmt, "P_1", document.issue_date.isoformat())
    if invoice.place and invoice.place.strip():
        _add(body, fmt, "P_1M", invoice.place.strip())
    _add(body, fmt, "P_2", invoice.number.strip())
    if invoice.sale_date is not None:
        _add(body, fmt, "P_6", invoice.sale_date.isoformat())

    for total in totals.by_rate:
        slot = RATE_SLOTS[total.rate]
        _add(body, fmt, slot.net_element, _money_text(total.net))
        if slot.vat_element is not None:
            _add(body, fmt, slot.vat_element, _money_text(total.vat))
    _add(body, fmt, "P_15", _money_text(totals.gross))

    annotations = _add(body, fmt, "Adnotacje")
    for name in ("P_16", "P_17", "P_18", "P_18A"):
        _add(annotations, fmt, name, "2")
    exemption = _add(annotations, fmt, "Zwolnienie")
    if totals.has_exempt_sales:
        _add(exemption, fmt, "P_19", "1")
        # P_19A = the Polish act (or a regulation issued under it) that grants the exemption.
        _add(exemption, fmt, "P_19A", (invoice.exemption_basis or "").strip())
    else:
        _add(exemption, fmt, "P_19N", "1")
    new_transport = _add(annotations, fmt, "NoweSrodkiTransportu")
    _add(new_transport, fmt, "P_22N", "1")
    _add(annotations, fmt, "P_23", "2")
    margin = _add(annotations, fmt, "PMarzy")
    _add(margin, fmt, "P_PMarzyN", "1")
    _add(body, fmt, "RodzajFaktury", "VAT")

    for index, line in enumerate(invoice.lines, start=1):
        row = _add(body, fmt, "FaWiersz")
        _add(row, fmt, "NrWierszaFa", str(index))
        _add(row, fmt, "P_7", line.description.strip())
        _add(row, fmt, "P_8A", line.unit.strip())
        _add(row, fmt, "P_8B", format(line.quantity, "f"))
        _add(row, fmt, "P_9A", _money_text(line.unit_net_price))
        _add(row, fmt, "P_11", _money_text(line.net))
        _add(row, fmt, "P_12", RATE_SLOTS[line.rate].p12)

    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    ).encode("utf-8")


def build(
    document: Document,
    invoice: Fa3Invoice,
    *,
    created_at: datetime | None = None,
    config_path: Path | None = None,
) -> bytes:
    """Build the FA(3) XML for ``document`` + ``invoice``.

    Raises :class:`Fa3TotalsMismatch` when the line items do not add up to the amounts
    stored on the document and :class:`Fa3ValidationError` for any other missing or invalid
    field; both carry the full ``violations`` list. The returned bytes are never uploaded
    to KSeF - see :data:`SYSTEM_INFO`.
    """
    fmt = schemas.fa3_format(config_path=config_path)
    totals = invoice.totals()
    violations, totals_mismatch = _collect(document, invoice, totals)
    if violations:
        error = Fa3TotalsMismatch if totals_mismatch else Fa3ValidationError
        raise error(violations)
    return _render(document, invoice, totals, fmt, created_at or datetime.now(UTC))

