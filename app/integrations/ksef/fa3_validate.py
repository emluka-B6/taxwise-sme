"""Structural validation of FA(3) invoice XML.

XSD validation (see :mod:`app.integrations.schemas`) proves that a document is *shaped*
like FA(3); it says nothing about the numbers inside it or about elements the schema marks
optional but an invoice cannot do without. This module checks the document on its own
terms:

* required elements (header metadata, both parties, ``P_15``, at least one ``FaWiersz``);
* types (dates, ``Decimal`` amounts with at most two fraction digits, integer row numbers,
  the ``P_12`` rate codes, the yes/no flags of ``Adnotacje``);
* total consistency (each ``P_13_x``/``P_14_x`` group against the lines at that rate, and
  ``P_15`` against the sum of the groups).

Every finding is a :class:`~app.integrations.schemas.Violation` with an element path, so
the API can return the whole list in one ``422`` response. The function never raises for
bad input: unparseable XML is a violation too.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from lxml import etree

from app.integrations import schemas
from app.integrations.ksef.fa3_builder import RATE_SLOTS, VatRate, is_valid_nip
from app.integrations.schemas import Violation

__all__ = ["EXTRA_CHECKS", "validate_fa3"]

_CENT: Final[Decimal] = Decimal("0.01")
_ZERO: Final[Decimal] = Decimal("0.00")
_KOD_WALUTY_RE: Final = re.compile(r"[A-Z]{3}")
_KOD_KRAJU_RE: Final = re.compile(r"[A-Z]{2}")
_VAT_YES_NO: Final[frozenset[str]] = frozenset({"1", "2"})
_RODZAJ_FAKTURY: Final[frozenset[str]] = frozenset(
    {"VAT", "KOR", "ZAL", "ROZ", "UPR", "KOR_ZAL", "KOR_ROZ"}
)

#: Short description of the checks this module performs (documented in the README).
EXTRA_CHECKS: Final[tuple[str, ...]] = (
    "required header/party/element presence",
    "date, decimal, integer and enumeration types",
    "P_13_x/P_14_x against the lines of that rate",
    "P_15 against the sum of the rate groups",
)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _parse_decimal(text: str | None) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _parse_date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _parse_timestamp(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


class _Checker:
    """Walks one FA(3) document and accumulates :class:`Violation` objects."""

    def __init__(self, fmt: schemas.Fa3Format) -> None:
        self.fmt = fmt
        self.violations: list[Violation] = []

    # -- helpers -----------------------------------------------------------------
    def _q(self, name: str) -> str:
        return f"{{{self.fmt.namespace}}}{name}"

    def find(self, parent: etree._Element, name: str) -> etree._Element | None:
        return parent.find(self._q(name))

    def text(self, parent: etree._Element, name: str) -> str | None:
        element = self.find(parent, name)
        if element is None or element.text is None:
            return None
        value = element.text.strip()
        return value or None

    def add(self, path: str, message: str) -> None:
        self.violations.append(Violation(path=path, message=message))

    def require(self, parent: etree._Element, name: str, path: str) -> str | None:
        value = self.text(parent, name)
        if value is None:
            self.add(f"{path}/{name}", "required element is missing or empty")
        return value

    def money(self, parent: etree._Element, name: str, path: str) -> Decimal | None:
        raw = self.text(parent, name)
        if raw is None:
            self.add(f"{path}/{name}", "required amount is missing")
            return None
        return self._amount(raw, f"{path}/{name}")

    def optional_money(self, parent: etree._Element, name: str, path: str) -> Decimal | None:
        if self.find(parent, name) is None:
            return None
        return self.money(parent, name, path)

    def _amount(self, raw: str, path: str) -> Decimal | None:
        value = _parse_decimal(raw)
        if value is None:
            self.add(path, f"{raw!r} is not a decimal amount")
            return None
        if value < _ZERO:
            self.add(path, "amount must not be negative")
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            self.add(path, "amount must have at most two fraction digits")
        return _money(value)

    # -- sections ----------------------------------------------------------------
    def header(self, root: etree._Element) -> None:
        header = self.find(root, "Naglowek")
        if header is None:
            self.add("Faktura/Naglowek", "required element is missing")
            return
        kod = self.find(header, "KodFormularza")
        if kod is None:
            self.add("Naglowek/KodFormularza", "required element is missing")
        else:
            if (kod.text or "").strip() != self.fmt.kod_formularza:
                self.add(
                    "Naglowek/KodFormularza",
                    f"expected text {self.fmt.kod_formularza!r}, got {kod.text!r}",
                )
            for attribute, expected in (
                ("kodSystemowy", self.fmt.kod_systemowy),
                ("wersjaSchemy", self.fmt.wersja_schemy),
            ):
                if kod.get(attribute) != expected:
                    self.add(
                        f"Naglowek/KodFormularza@{attribute}",
                        f"expected {expected!r}, got {kod.get(attribute)!r}",
                    )
        variant = self.require(header, "WariantFormularza", "Naglowek")
        if variant is not None and variant != str(self.fmt.wariant_formularza):
            self.add(
                "Naglowek/WariantFormularza",
                f"expected {self.fmt.wariant_formularza}, got {variant!r}",
            )
        stamp = self.require(header, "DataWytworzeniaFa", "Naglowek")
        if stamp is not None and _parse_timestamp(stamp) is None:
            self.add("Naglowek/DataWytworzeniaFa", f"{stamp!r} is not an ISO-8601 timestamp")

    def party(self, root: etree._Element, name: str, *, flags: bool) -> None:
        block = self.find(root, name)
        if block is None:
            self.add(f"Faktura/{name}", "required element is missing")
            return
        ident = self.find(block, "DaneIdentyfikacyjne")
        if ident is None:
            self.add(f"{name}/DaneIdentyfikacyjne", "required element is missing")
            ident = block
        nip = self.require(ident, "NIP", f"{name}/DaneIdentyfikacyjne")
        if nip is not None and not is_valid_nip(nip):
            self.add(f"{name}/DaneIdentyfikacyjne/NIP", f"{nip!r} is not a valid Polish NIP")
        self.require(ident, "Nazwa", f"{name}/DaneIdentyfikacyjne")
        address = self.find(block, "Adres")
        if address is None:
            self.add(f"{name}/Adres", "required element is missing")
        else:
            country = self.require(address, "KodKraju", f"{name}/Adres")
            if country is not None and not _KOD_KRAJU_RE.match(country):
                self.add(f"{name}/Adres/KodKraju", f"{country!r} is not a two-letter country code")
            self.require(address, "AdresL1", f"{name}/Adres")
        if flags:
            for flag in ("JST", "GV"):
                value = self.require(block, flag, name)
                if value is not None and value not in _VAT_YES_NO:
                    self.add(f"{name}/{flag}", f"{flag} must be '1' or '2', got {value!r}")

    def lines(self, fa: etree._Element) -> tuple[dict[VatRate, Decimal], set[VatRate]]:
        rows = fa.findall(self._q("FaWiersz"))
        sums: dict[VatRate, Decimal] = {}
        seen: set[int] = set()
        if not rows:
            self.add("Fa/FaWiersz", "at least one line item is required")
            return sums, set()
        if len(rows) > 10_000:
            self.add("Fa/FaWiersz", "at most 10000 line items are allowed")
        for position, row in enumerate(rows, start=1):
            path = f"Fa/FaWiersz[{position}]"
            number = self.require(row, "NrWierszaFa", path)
            if number is not None:
                try:
                    parsed = int(number)
                except ValueError:
                    self.add(f"{path}/NrWierszaFa", f"{number!r} is not an integer")
                else:
                    if parsed < 1:
                        self.add(f"{path}/NrWierszaFa", "line number must be at least 1")
                    if parsed in seen:
                        self.add(f"{path}/NrWierszaFa", f"line number {parsed} is duplicated")
                    seen.add(parsed)
            description = self.require(row, "P_7", path)
            if description is not None and len(description) > 512:
                self.add(f"{path}/P_7", "description must not exceed 512 characters")
            self.require(row, "P_8A", path)
            quantity_raw = self.text(row, "P_8B")
            quantity = _parse_decimal(quantity_raw)
            if quantity_raw is None:
                self.add(f"{path}/P_8B", "required quantity is missing")
            elif quantity is None or quantity <= _ZERO:
                self.add(f"{path}/P_8B", f"{quantity_raw!r} is not a positive quantity")
            if self.find(row, "P_9A") is not None:
                self.optional_money(row, "P_9A", path)
            net = self.money(row, "P_11", path)
            code = self.require(row, "P_12", path)
            rate = next((item for item, slot in RATE_SLOTS.items() if slot.p12 == code), None)
            if code is not None and rate is None:
                self.add(f"{path}/P_12", f"{code!r} is not a supported FA(3) rate code")
            if rate is not None and net is not None:
                sums[rate] = sums.get(rate, _ZERO) + net
        return sums, set(sums)


    def rate_groups(
        self, fa: etree._Element, sums: dict[VatRate, Decimal]
    ) -> tuple[Decimal, Decimal]:
        """Check ``P_13_x``/``P_14_x`` against the lines; return written net and VAT."""
        written_net = _ZERO
        written_vat = _ZERO
        groups = 0
        for rate in VatRate:
            slot = RATE_SLOTS[rate]
            expected_net = _money(sums.get(rate, _ZERO))
            net = self.optional_money(fa, slot.net_element, "Fa")
            if net is None:
                if expected_net != _ZERO:
                    self.add(
                        f"Fa/{slot.net_element}",
                        f"{slot.net_element} is missing but lines at {slot.p12} add up to "
                        f"{expected_net}",
                    )
                continue
            groups += 1
            written_net += net
            if net != expected_net:
                self.add(
                    f"Fa/{slot.net_element}",
                    f"{net} does not match the net of the lines at {slot.p12} ({expected_net})",
                )
            if slot.vat_element is None:
                continue
            percent = rate.percent
            expected_vat = _ZERO if percent is None else _money(expected_net * percent / 100)
            vat = self.optional_money(fa, slot.vat_element, "Fa")
            if vat is None:
                self.add(f"Fa/{slot.vat_element}", f"required amount is missing for {slot.p12}")
                continue
            written_vat += vat
            if vat != expected_vat:
                self.add(
                    f"Fa/{slot.vat_element}",
                    f"{vat} does not match {percent}% of the net at {slot.p12} "
                    f"({expected_vat})",
                )
        if groups == 0:
            self.add("Fa", "at least one P_13_* summary element is required")
        return _money(written_net), _money(written_vat)

    def annotations(self, fa: etree._Element, *, exempt: bool) -> None:
        block = self.find(fa, "Adnotacje")
        if block is None:
            self.add("Fa/Adnotacje", "required element is missing")
            return
        for name in ("P_16", "P_17", "P_18", "P_18A", "P_23"):
            value = self.require(block, name, "Fa/Adnotacje")
            if value is not None and value not in _VAT_YES_NO:
                self.add(f"Fa/Adnotacje/{name}", f"{name} must be '1' or '2', got {value!r}")
        exemption = self.find(block, "Zwolnienie")
        if exemption is None:
            self.add("Fa/Adnotacje/Zwolnienie", "required element is missing")
        else:
            flag = self.text(exemption, "P_19")
            basis = (
                self.text(exemption, "P_19A")
                or self.text(exemption, "P_19B")
                or self.text(exemption, "P_19C")
            )
            not_applicable = self.text(exemption, "P_19N")
            if exempt:
                if flag != "1" or basis is None:
                    self.add(
                        "Fa/Adnotacje/Zwolnienie/P_19",
                        "exempt (zw) lines require P_19=1 and the legal basis in "
                        "P_19A/P_19B/P_19C",
                    )
            elif flag is not None or basis is not None:
                self.add(
                    "Fa/Adnotacje/Zwolnienie/P_19",
                    "an exemption is declared but no line uses the zw rate",
                )
            elif not_applicable != "1":
                self.add(
                    "Fa/Adnotacje/Zwolnienie", "either P_19 (with a basis) or P_19N=1 is required"
                )
        transport = self.find(block, "NoweSrodkiTransportu")
        if transport is None:
            self.add("Fa/Adnotacje/NoweSrodkiTransportu", "required element is missing")
        elif self.text(transport, "P_22N") is None and self.find(transport, "P_22") is None:
            self.add(
                "Fa/Adnotacje/NoweSrodkiTransportu", "either P_22 or P_22N is required"
            )
        margin = self.find(block, "PMarzy")
        if margin is None:
            self.add("Fa/Adnotacje/PMarzy", "required element is missing")
        elif self.text(margin, "P_PMarzyN") is None and self.find(margin, "P_PMarzy") is None:
            self.add("Fa/Adnotacje/PMarzy", "either P_PMarzy or P_PMarzyN is required")

    def body(self, root: etree._Element) -> None:
        fa = self.find(root, "Fa")
        if fa is None:
            self.add("Faktura/Fa", "required element is missing")
            return
        currency = self.require(fa, "KodWaluty", "Fa")
        if currency is not None and not _KOD_WALUTY_RE.fullmatch(currency):
            self.add("Fa/KodWaluty", f"{currency!r} is not a three-letter currency code")
        issued = self.require(fa, "P_1", "Fa")
        if issued is not None and _parse_date(issued) is None:
            self.add("Fa/P_1", f"{issued!r} is not an ISO-8601 date")
        place = self.text(fa, "P_1M")
        if place is not None and len(place) > 256:
            self.add("Fa/P_1M", "place of issue must not exceed 256 characters")
        number = self.require(fa, "P_2", "Fa")
        if number is not None and len(number) > 256:
            self.add("Fa/P_2", "invoice number must not exceed 256 characters")
        sale_date = self.text(fa, "P_6")
        if sale_date is not None and _parse_date(sale_date) is None:
            self.add("Fa/P_6", f"{sale_date!r} is not an ISO-8601 date")
        kind = self.require(fa, "RodzajFaktury", "Fa")
        if kind is not None and kind not in _RODZAJ_FAKTURY:
            self.add("Fa/RodzajFaktury", f"{kind!r} is not a known invoice kind")

        sums, rates = self.lines(fa)
        net, vat = self.rate_groups(fa, sums)
        self.annotations(fa, exempt=VatRate.ZW in rates)
        total = self.money(fa, "P_15", "Fa")
        if total is not None and total != _money(net + vat):
            self.add(
                "Fa/P_15",
                f"{total} does not match the sum of the P_13_*/P_14_* groups "
                f"({_money(net + vat)})",
            )



def validate_fa3(
    xml_bytes: bytes,
    *,
    fmt: schemas.Fa3Format | None = None,
    config_path: Path | None = None,
) -> list[Violation]:
    """Structurally validate an FA(3) document; returns every finding (empty = valid).

    ``fmt`` defaults to the registry entry, so the expected namespace and the
    ``KodFormularza`` attributes are never duplicated in test or caller code.
    """
    expected_format = fmt or schemas.fa3_format(config_path=config_path)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        return [Violation(path="document", message=f"not well-formed XML: {exc}")]

    checker = _Checker(expected_format)
    expected_root = f"{{{expected_format.namespace}}}Faktura"
    if root.tag != expected_root:
        checker.add("Faktura", f"root element must be {expected_root}, got {root.tag!r}")
        return checker.violations
    checker.header(root)
    checker.party(root, "Podmiot1", flags=False)
    checker.party(root, "Podmiot2", flags=True)
    checker.body(root)
    return checker.violations

