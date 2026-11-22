"""Provider-independent invoice import contract.

Every accounting provider (CSV export, REST API, ...) must implement
:class:`AccountingProvider`: it returns platform invoices as :class:`InvoiceDTO`
objects for a given ``since`` date. The DTO carries the stable ``external_id`` of
the source system, which together with ``source`` makes imports idempotent
(``UNIQUE(source, external_id)`` on ``documents``).

Parsing helpers live here as well so that every provider applies the same rules:

* money is parsed with Polish and international separators and quantised to
  grosze (``Decimal`` - never ``float``);
* dates must match one of the formats declared by the provider mapping;
* a NIP is normalised to ten digits or reported as invalid;
* the document type comes from a mapping alias, a ``DocumentType`` value or a
  configured default.

A provider never silently drops a malformed row: :class:`CsvProvider` records
:class:`ImportRowError` entries and keeps importing the remaining rows.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Protocol, runtime_checkable

from pydantic import SecretStr

from app.domain.enums import DocumentSource, DocumentType

__all__ = [
    "DEFAULT_DATE_FORMATS",
    "AccountingProvider",
    "HttpClient",
    "ImportRowError",
    "InvoiceDTO",
    "InvoiceValidationError",
    "ProviderApiError",
    "ProviderConfigurationError",
    "invoice_from_fields",
    "normalise_nip",
    "normalise_period",
    "parse_date",
    "parse_money",
    "secret_value",
]

#: Formats accepted unless a provider mapping declares its own list.
DEFAULT_DATE_FORMATS: Final[tuple[str, ...]] = ("%Y-%m-%d", "%d.%m.%Y")

_CENT: Final[Decimal] = Decimal("0.01")

#: ``YYYY-MM`` - the period format stored on ``documents.period``.
_PERIOD_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")

#: A Polish NIP has exactly ten digits; separators and a ``PL`` prefix are accepted.
_NIP_DIGITS: Final[int] = 10


class InvoiceValidationError(ValueError):
    """A single invoice cannot be imported (bad amount, date, NIP, ...)."""


class ProviderConfigurationError(RuntimeError):
    """A provider is missing its environment configuration (URL, token, ...)."""


class ProviderApiError(RuntimeError):
    """The remote accounting API could not be reached or returned unusable data."""


@dataclass(frozen=True, slots=True)
class InvoiceDTO:
    """One invoice as reported by an accounting platform.

    ``source`` + ``external_id`` identify the row; everything else is normalised
    to the units used by the ``documents`` table.
    """

    source: DocumentSource
    external_id: str
    document_type: DocumentType
    issue_date: date
    period: str
    amount_net: Decimal | None
    vat_amount: Decimal | None
    amount_gross: Decimal
    currency: str = "PLN"
    counterparty_nip: str | None = None


@dataclass(frozen=True, slots=True)
class ImportRowError:
    """A row that was rejected without aborting the import.

    ``row`` is the physical line (or record ordinal for an API payload) so the
    user can find the offending entry in the source file.
    """

    row: int
    message: str
    external_id: str | None = None


@runtime_checkable
class AccountingProvider(Protocol):
    """Common surface of an invoice source integration."""

    source: DocumentSource

    def fetch_invoices(self, since: date | None = None) -> list[InvoiceDTO]:
        """Return invoices issued on or after ``since`` (all invoices when ``None``)."""
        ...


def _text(value: object | None) -> str:
    """Trimmed string form of a CSV/API value (``None`` becomes ``""``)."""
    if value is None:
        return ""
    return str(value).strip()


def parse_money(value: object) -> Decimal:
    """Parse a money value and quantise it to grosze.

    Accepts ``1230.00``, ``1230,00``, ``1 230,00``, ``PLN 1 230,00`` and
    accounting-style parentheses for negatives. Raises
    :class:`InvoiceValidationError` for anything that is not a finite number.
    """
    if isinstance(value, Decimal):
        amount = value
    else:
        text = _text(value)
        if not text:
            raise InvoiceValidationError("amount is empty")
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        # Remove spaces (including NBSP/narrow NBSP), currencies and other noise
        # while keeping the separators that identify the decimal point.
        text = text.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
        text = re.sub(r"[^0-9,.\-+]", "", text)
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        if text.count(".") > 1:
            raise InvoiceValidationError(f"{value!r} is not a valid amount")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise InvoiceValidationError(f"{value!r} is not a valid amount") from exc
        if negative:
            amount = -amount
    if not amount.is_finite():
        raise InvoiceValidationError(f"{value!r} is not a finite amount")
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def parse_date(value: object, formats: Sequence[str]) -> date:
    """Parse ``value`` using the first matching strptime format."""
    text = _text(value)
    if not text:
        raise InvoiceValidationError("issue_date is required")
    if not formats:
        raise InvoiceValidationError("no date formats are configured")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise InvoiceValidationError(
        f"issue_date {text!r} does not match any configured format: {', '.join(formats)}"
    )


def normalise_period(value: object | None, issue_date: date) -> str:
    """Return a ``YYYY-MM`` period; derive it from ``issue_date`` when absent."""
    text = _text(value)
    if not text:
        return f"{issue_date:%Y-%m}"
    if _PERIOD_PATTERN.fullmatch(text) is None:
        raise InvoiceValidationError(f"period {text!r} must use the YYYY-MM format")
    return text


def normalise_nip(value: object | None) -> str | None:
    """Normalise a NIP to ten digits; ``None`` for an empty value."""
    text = _text(value)
    if not text:
        return None
    upper = text.upper()
    if upper.startswith("PL"):
        upper = upper[2:]
    if re.search(r"[A-Z]", upper):
        raise InvoiceValidationError(f"NIP {text!r} must contain digits only")
    digits = re.sub(r"[\s-]", "", upper)
    if not digits.isdigit() or len(digits) != _NIP_DIGITS:
        raise InvoiceValidationError(f"NIP {text!r} must contain exactly ten digits")
    return digits


def _optional_amount(fields: Mapping[str, object], name: str) -> Decimal | None:
    raw = fields.get(name)
    if raw is None or not _text(raw):
        return None
    try:
        return parse_money(raw)
    except InvoiceValidationError as exc:
        raise InvoiceValidationError(f"{name}: {exc}") from exc


def _document_type(
    raw: object | None,
    *,
    default: DocumentType | None,
    aliases: Mapping[str, DocumentType] | None,
) -> DocumentType:
    text = _text(raw)
    if not text:
        if default is None:
            raise InvoiceValidationError("document_type is required")
        return default
    key = " ".join(text.split()).casefold()
    for alias, target in (aliases or {}).items():
        if " ".join(alias.split()).casefold() == key:
            return target
    try:
        return DocumentType(text)
    except ValueError as exc:
        raise InvoiceValidationError(f"document_type {text!r} is not recognised") from exc


def _currency(value: object | None, default: str) -> str:
    text = _text(value).upper() or default.upper()
    if re.fullmatch(r"[A-Z]{3}", text) is None:
        raise InvoiceValidationError(f"currency {text!r} must be a three-letter code")
    return text


def invoice_from_fields(
    source: DocumentSource,
    fields: Mapping[str, object],
    *,
    date_formats: Sequence[str] = DEFAULT_DATE_FORMATS,
    default_document_type: DocumentType | None = DocumentType.VAT_INVOICE_COST,
    document_types: Mapping[str, DocumentType] | None = None,
    default_currency: str = "PLN",
) -> InvoiceDTO:
    """Build a validated :class:`InvoiceDTO` from canonical field names.

    Raises :class:`InvoiceValidationError` with a human-readable reason when the
    record cannot be imported; callers record that reason and continue with the
    next row.
    """
    external_id = _text(fields.get("external_id"))
    if not external_id:
        raise InvoiceValidationError("external_id is required")
    if len(external_id) > 128:
        raise InvoiceValidationError("external_id must be at most 128 characters")

    issue_date = parse_date(fields.get("issue_date"), date_formats)
    period = normalise_period(fields.get("period"), issue_date)

    amount_net = _optional_amount(fields, "amount_net")
    vat_amount = _optional_amount(fields, "vat_amount")
    gross_raw = fields.get("amount_gross")
    if gross_raw is not None and _text(gross_raw):
        try:
            amount_gross = parse_money(gross_raw)
        except InvoiceValidationError as exc:
            raise InvoiceValidationError(f"amount_gross: {exc}") from exc
    elif amount_net is not None and vat_amount is not None:
        amount_gross = (amount_net + vat_amount).quantize(_CENT, rounding=ROUND_HALF_UP)
    else:
        raise InvoiceValidationError(
            "amount_gross is required (or provide both amount_net and vat_amount)"
        )

    return InvoiceDTO(
        source=source,
        external_id=external_id,
        document_type=_document_type(
            fields.get("document_type"),
            default=default_document_type,
            aliases=document_types,
        ),
        issue_date=issue_date,
        period=period,
        amount_net=amount_net,
        vat_amount=vat_amount,
        amount_gross=amount_gross,
        currency=_currency(fields.get("currency"), default_currency),
        counterparty_nip=normalise_nip(fields.get("counterparty_nip")),
    )


def secret_value(token: SecretStr | str | None) -> SecretStr | None:
    """Normalise a token argument to ``SecretStr`` (``str`` is treated as a secret)."""
    if token is None:
        return None
    if isinstance(token, SecretStr):
        return token
    return SecretStr(token)



@runtime_checkable
class HttpClient(Protocol):
    """Minimal GET client used by the API provider.

    Injecting the client keeps the provider testable without a network stack; the
    default implementation is a small ``urllib`` adapter. Implementations raise
    :class:`ProviderApiError` (or ``OSError``) on transport failures.
    """

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> bytes:
        """Perform a GET request and return the response body."""
        ...
