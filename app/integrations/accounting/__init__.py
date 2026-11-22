"""Accounting-platform invoice importers.

The public surface mirrors the two shipped strategies:

* :class:`CsvProvider` - an export downloaded from inFakt / Fakturownia / wFirma,
  parsed through a versioned YAML column mapping;
* :class:`ApiProvider` - a skeleton REST client whose URL/token come from ENV.

Both implement :class:`AccountingProvider` and return :class:`InvoiceDTO` rows.
"""

from app.integrations.accounting.api_provider import (
    ApiProvider,
    UrllibHttpClient,
    build_api_provider,
)
from app.integrations.accounting.base import (
    DEFAULT_DATE_FORMATS,
    AccountingProvider,
    HttpClient,
    ImportRowError,
    InvoiceDTO,
    InvoiceValidationError,
    ProviderApiError,
    ProviderConfigurationError,
)
from app.integrations.accounting.csv_provider import (
    PROVIDER_SOURCES,
    CsvFormatError,
    CsvMapping,
    CsvProvider,
    MappingConfigError,
    UnknownProviderError,
    build_csv_provider,
    load_mapping,
    supported_providers,
)

__all__ = [
    "DEFAULT_DATE_FORMATS",
    "PROVIDER_SOURCES",
    "AccountingProvider",
    "ApiProvider",
    "CsvFormatError",
    "CsvMapping",
    "CsvProvider",
    "HttpClient",
    "ImportRowError",
    "InvoiceDTO",
    "InvoiceValidationError",
    "MappingConfigError",
    "ProviderApiError",
    "ProviderConfigurationError",
    "UnknownProviderError",
    "UrllibHttpClient",
    "build_api_provider",
    "build_csv_provider",
    "load_mapping",
    "supported_providers",
]
