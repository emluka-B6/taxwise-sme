"""CSV invoice imports driven by a versioned YAML column mapping.

The mapping files in ``config/import_mappings/<provider>.yaml`` are the single
source of truth for the column names used by inFakt, Fakturownia and wFirma.
Changing a vendor export therefore changes one YAML file (`version` must be
bumped) instead of Python code. A mapping declares:

* ``version`` and the provider/source it belongs to;
* the CSV dialect (``delimiter``, ``encoding``) and accepted date formats;
* canonical field -> list of header aliases;
* the aliases used for sales/cost document types and their default.

:class:`CsvProvider` rejects a row it cannot parse but keeps importing the rest;
the rejected rows are available as :attr:`CsvProvider.row_errors` and are
reported by ``POST /api/imports/{provider}`` as ``errors[]``.
"""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.enums import DocumentSource, DocumentType
from app.integrations.accounting.base import (
    DEFAULT_DATE_FORMATS,
    ImportRowError,
    InvoiceDTO,
    InvoiceValidationError,
    invoice_from_fields,
)

__all__ = [
    "COLUMN_FIELDS",
    "MAPPINGS_DIR",
    "PROVIDER_SOURCES",
    "REQUIRED_COLUMN_FIELDS",
    "CsvFormatError",
    "CsvMapping",
    "CsvProvider",
    "DocumentTypeConfig",
    "MappingConfigError",
    "UnknownProviderError",
    "build_csv_provider",
    "load_mapping",
    "supported_providers",
]

#: Repository root; mapping files are resolved relative to it.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: Shipped column mappings (``config/import_mappings``).
MAPPINGS_DIR: Final[Path] = PROJECT_ROOT / "config" / "import_mappings"

#: Provider name -> persisted ``DocumentSource``.
PROVIDER_SOURCES: Final[Mapping[str, DocumentSource]] = {
    "infakt": DocumentSource.IMPORT_INFAKT,
    "fakturownia": DocumentSource.IMPORT_FAKTUROWNIA,
    "wfirma": DocumentSource.IMPORT_WFIRMA,
}

#: Canonical invoice fields a mapping may address.
COLUMN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "external_id",
        "issue_date",
        "period",
        "amount_net",
        "vat_amount",
        "amount_gross",
        "currency",
        "counterparty_nip",
        "document_type",
    }
)

#: A usable mapping must at least identify the invoice, its date and its gross amount.
REQUIRED_COLUMN_FIELDS: Final[frozenset[str]] = frozenset(
    {"external_id", "issue_date", "amount_gross"}
)


def _normalise_header(value: str) -> str:
    """Header comparison ignores case and repeated whitespace."""
    return " ".join(value.split()).casefold()


class MappingConfigError(RuntimeError):
    """``config/import_mappings/*.yaml`` is missing or malformed."""


class CsvFormatError(ValueError):
    """The uploaded CSV cannot be decoded or does not have the required columns."""


class UnknownProviderError(LookupError):
    """The requested provider has no CSV mapping."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.supported = supported_providers()
        super().__init__(
            f"unknown provider {provider!r}; supported providers: {', '.join(self.supported)}"
        )


class DocumentTypeConfig(BaseModel):
    """Mapping of vendor-specific document type labels to domain values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: DocumentType = DocumentType.VAT_INVOICE_COST
    values: dict[str, DocumentType] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _normalise_aliases(cls, value: dict[str, DocumentType]) -> dict[str, DocumentType]:
        normalised: dict[str, DocumentType] = {}
        for alias, target in value.items():
            key = _normalise_header(alias)
            if not key:
                raise ValueError("document type aliases must not be empty")
            previous = normalised.get(key)
            if previous is not None and previous is not target:
                raise ValueError(f"document type alias {alias!r} maps to two values")
            normalised[key] = target
        return normalised


class CsvMapping(BaseModel):
    """Validated content of one ``config/import_mappings/<provider>.yaml`` file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Annotated[int, Field(ge=1)]
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    source: DocumentSource
    delimiter: Annotated[str, Field(min_length=1, max_length=1)] = ";"
    encoding: Annotated[str, Field(min_length=1, max_length=64)] = "utf-8-sig"
    date_formats: Annotated[tuple[str, ...], Field(min_length=1)] = DEFAULT_DATE_FORMATS
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "PLN"
    columns: dict[str, tuple[str, ...]]
    document_type: DocumentTypeConfig = Field(default_factory=DocumentTypeConfig)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("encoding")
    @classmethod
    def _known_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unknown encoding {value!r}") from exc
        return value

    @field_validator("currency")
    @classmethod
    def _uppercase_currency(cls, value: str) -> str:
        currency = value.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        return currency

    @field_validator("date_formats")
    @classmethod
    def _check_date_formats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for fmt in value:
            try:
                sample = datetime(2026, 3, 4).strftime(fmt)
                datetime.strptime(sample, fmt)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid date format {fmt!r}") from exc
        return value

    @field_validator("columns")
    @classmethod
    def _check_columns(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        unknown = sorted(set(value) - COLUMN_FIELDS)
        if unknown:
            raise ValueError(f"unknown column targets: {unknown}")
        missing = sorted(REQUIRED_COLUMN_FIELDS - set(value))
        if missing:
            raise ValueError(f"missing required column targets: {missing}")

        aliases: dict[str, str] = {}
        normalised: dict[str, tuple[str, ...]] = {}
        for field, field_aliases in value.items():
            clean_aliases = tuple(alias.strip() for alias in field_aliases)
            if not clean_aliases or any(not alias for alias in clean_aliases):
                raise ValueError(f"column target {field!r} must list non-empty header aliases")
            for alias in clean_aliases:
                key = _normalise_header(alias)
                previous = aliases.get(key)
                if previous is not None and previous != field:
                    raise ValueError(
                        f"header {alias!r} is mapped to both {previous!r} and {field!r}"
                    )
                aliases[key] = field
            normalised[field] = clean_aliases
        return normalised


def supported_providers() -> tuple[str, ...]:
    """Names accepted by ``POST /api/imports/{provider}`` (sorted)."""
    return tuple(sorted(PROVIDER_SOURCES))


def load_mapping(path: Path) -> CsvMapping:
    """Read and validate one YAML mapping file (never cached - edits are picked up)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MappingConfigError(f"cannot read import mapping {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MappingConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise MappingConfigError(f"{path}: expected a mapping at the top level")
    try:
        return CsvMapping.model_validate(raw)
    except ValidationError as exc:
        raise MappingConfigError(f"{path}: {exc}") from exc


class CsvProvider:
    """Invoice provider backed by a vendor CSV export and a YAML column mapping."""

    def __init__(
        self,
        mapping: CsvMapping,
        data: bytes | str | None = None,
    ) -> None:
        self.mapping = mapping
        self.source = mapping.source
        self.mapping_version = mapping.version
        self.row_errors: list[ImportRowError] = []
        self._data = data

    @classmethod
    def from_mapping_file(
        cls,
        path: Path,
        data: bytes | str | None = None,
        *,
        expected_provider: str | None = None,
        expected_source: DocumentSource | None = None,
    ) -> CsvProvider:
        """Build a provider from ``path``, checking its declared provider/source."""
        mapping = load_mapping(path)
        if expected_provider is not None and mapping.provider != expected_provider:
            raise MappingConfigError(
                f"{path}: declares provider {mapping.provider!r}, expected {expected_provider!r}"
            )
        if expected_source is not None and mapping.source is not expected_source:
            raise MappingConfigError(
                f"{path}: declares source {mapping.source.value!r}, "
                f"expected {expected_source.value!r}"
            )
        return cls(mapping, data)

    @classmethod
    def from_provider(
        cls,
        provider: str,
        data: bytes | str | None = None,
        *,
        mapping_dir: Path | None = None,
    ) -> CsvProvider:
        """Resolve a provider name to ``config/import_mappings/<provider>.yaml``."""
        name = provider.strip().lower()
        source = PROVIDER_SOURCES.get(name)
        if source is None:
            raise UnknownProviderError(name)
        path = (mapping_dir or MAPPINGS_DIR) / f"{name}.yaml"
        return cls.from_mapping_file(
            path,
            data,
            expected_provider=name,
            expected_source=source,
        )

    @property
    def errors(self) -> tuple[ImportRowError, ...]:
        """Immutable view of the rows rejected during the last fetch."""
        return tuple(self.row_errors)

    def fetch_invoices(self, since: date | None = None) -> list[InvoiceDTO]:
        """Parse the loaded CSV; invalid rows are collected in :attr:`row_errors`."""
        self.row_errors.clear()
        if self._data is None:
            raise CsvFormatError("no CSV content was supplied to the provider")

        text = self._decode(self._data)
        if not text.strip():
            raise CsvFormatError("the uploaded CSV file is empty")

        reader = csv.DictReader(
            io.StringIO(text, newline=""),
            delimiter=self.mapping.delimiter,
            quotechar='"',
            skipinitialspace=True,
        )
        if reader.fieldnames is None:
            raise CsvFormatError("the uploaded CSV file has no header row")
        reader.fieldnames = [name.strip() for name in reader.fieldnames]
        resolved = self._resolve_columns(reader.fieldnames)

        invoices: list[InvoiceDTO] = []
        for raw_row in reader:
            line = reader.line_num
            if _row_is_blank(raw_row):
                continue
            fields = {
                target: raw_row.get(header) for target, header in resolved.items()
            }
            try:
                invoice = invoice_from_fields(
                    self.source,
                    fields,
                    date_formats=self.mapping.date_formats,
                    default_document_type=self.mapping.document_type.default,
                    document_types=self.mapping.document_type.values,
                    default_currency=self.mapping.currency,
                )
            except InvoiceValidationError as exc:
                self.row_errors.append(
                    ImportRowError(
                        row=line,
                        message=str(exc),
                        external_id=_external_id(raw_row, resolved),
                    )
                )
                continue
            if since is not None and invoice.issue_date < since:
                continue
            invoices.append(invoice)
        return invoices

    def _decode(self, data: bytes | str) -> str:
        if isinstance(data, str):
            return data
        try:
            return data.decode(self.mapping.encoding)
        except UnicodeDecodeError as exc:
            raise CsvFormatError(
                f"the CSV file is not valid {self.mapping.encoding} text"
            ) from exc

    def _resolve_columns(self, headers: list[str]) -> dict[str, str]:
        lookup = {_normalise_header(header): header for header in headers}
        resolved: dict[str, str] = {}
        missing: list[str] = []
        for target, aliases in self.mapping.columns.items():
            for alias in aliases:
                header = lookup.get(_normalise_header(alias))
                if header is not None:
                    resolved[target] = header
                    break
            else:
                if target in REQUIRED_COLUMN_FIELDS:
                    missing.append(target)
        if missing:
            raise CsvFormatError(
                f"the CSV file is missing required columns: {', '.join(sorted(missing))}"
            )
        return resolved


def _row_is_blank(row: Mapping[str | None, str | None]) -> bool:
    """Ignore blank lines instead of reporting them as invalid invoices."""
    return not any(
        isinstance(value, str) and value.strip()
        for key, value in row.items()
        if key is not None
    )


def _external_id(
    row: Mapping[str | None, str | None], resolved: Mapping[str, str]
) -> str | None:
    header = resolved.get("external_id")
    if header is None:
        return None
    value = row.get(header)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def build_csv_provider(
    provider: str,
    data: bytes | str | None = None,
    *,
    mapping_dir: Path | None = None,
) -> CsvProvider:
    """Factory used by the API: name + uploaded bytes -> configured provider."""
    return CsvProvider.from_provider(provider, data, mapping_dir=mapping_dir)
