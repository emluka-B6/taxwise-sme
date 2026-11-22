"""REST API invoice provider skeleton (URL and token from the environment).

The provider is intentionally thin: it performs **one** authenticated GET to the
configured invoices endpoint, maps the JSON records through the same validation
helpers as the CSV importers and returns :class:`InvoiceDTO` objects. Field
names are the canonical ones (``external_id``, ``issue_date``,
``amount_gross``, ...), so a vendor-specific adapter only has to translate its
payload shape.

Security properties:

* there is no default URL - ``ACCOUNTING_API_URL`` is required;
* the token is read from ``ACCOUNTING_API_TOKEN`` only and held as ``SecretStr``;
* :meth:`ApiProvider.fetch_invoices` validates the configuration *before* it
  touches the HTTP client, so a missing token/URL raises
  :class:`ProviderConfigurationError` and performs zero network calls.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import date
from typing import Final

from pydantic import SecretStr

from app.domain.enums import DocumentSource
from app.integrations.accounting.base import (
    DEFAULT_DATE_FORMATS,
    HttpClient,
    ImportRowError,
    InvoiceDTO,
    InvoiceValidationError,
    ProviderApiError,
    ProviderConfigurationError,
    invoice_from_fields,
    secret_value,
)
from app.settings import Settings, get_settings

__all__ = [
    "MAX_API_RESPONSE_BYTES",
    "ApiProvider",
    "UrllibHttpClient",
    "build_api_provider",
]

#: Hard cap on a response body; protects the process from a runaway provider.
MAX_API_RESPONSE_BYTES: Final[int] = 8 * 1024 * 1024

#: Environment variable names surfaced in configuration errors.
URL_ENV: Final[str] = "ACCOUNTING_API_URL"
TOKEN_ENV: Final[str] = "ACCOUNTING_API_TOKEN"


class UrllibHttpClient:
    """Default :class:`HttpClient` implementation using only the stdlib."""

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> bytes:
        request = urllib.request.Request(url, method="GET", headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(MAX_API_RESPONSE_BYTES)  # type: ignore[no-any-return]
        except (urllib.error.URLError, OSError) as exc:
            # Never include the request (and thus the token) in the error.
            raise ProviderApiError("the invoice API request failed") from exc


class ApiProvider:
    """Skeleton REST provider; a vendor adapter supplies the URL and token via ENV."""

    def __init__(
        self,
        *,
        source: DocumentSource,
        base_url: str | None,
        token: SecretStr | str | None,
        endpoint: str = "invoices",
        timeout: float = 30.0,
        http_client: HttpClient | None = None,
        date_formats: tuple[str, ...] = DEFAULT_DATE_FORMATS,
    ) -> None:
        self.source = source
        self.row_errors: list[ImportRowError] = []
        self._base_url = base_url
        self._token = secret_value(token)
        self._endpoint = endpoint
        self._timeout = timeout
        self._http_client = http_client
        self._date_formats = date_formats

    def validate_configuration(self) -> None:
        """Raise :class:`ProviderConfigurationError` unless URL and token are usable."""
        missing: list[str] = []
        if self._base_url is None or not self._base_url.strip():
            missing.append(URL_ENV)
        if self._token is None or not self._token.get_secret_value().strip():
            missing.append(TOKEN_ENV)
        if missing:
            raise ProviderConfigurationError(
                "missing invoice API configuration: " + ", ".join(missing)
            )

        base_url = self._base_url.strip() if self._base_url is not None else ""
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigurationError(
                f"{URL_ENV} must be an absolute http(s) URL, got {base_url!r}"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ProviderConfigurationError(
                f"{URL_ENV} must not embed credentials; use {TOKEN_ENV} instead"
            )
        endpoint = self._endpoint.strip()
        if not endpoint or "://" in endpoint:
            raise ProviderConfigurationError(
                f"the invoices endpoint must be a path, got {self._endpoint!r}"
            )


    def fetch_invoices(self, since: date | None = None) -> list[InvoiceDTO]:
        """Fetch and validate invoices; configuration is checked before any request."""
        self.validate_configuration()
        self.row_errors.clear()

        # After validation both values are present; the assertions keep mypy honest.
        assert self._base_url is not None
        assert self._token is not None
        client = self._http_client or UrllibHttpClient()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token.get_secret_value()}",
        }
        try:
            raw = client.get(self._invoices_url(since), headers=headers, timeout=self._timeout)
        except ProviderApiError:
            raise
        except OSError as exc:
            raise ProviderApiError("the invoice API request failed") from exc

        records = self._records(self._decode(raw))
        invoices: list[InvoiceDTO] = []
        for ordinal, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                self.row_errors.append(
                    ImportRowError(
                        row=ordinal,
                        message="the API record is not a JSON object",
                    )
                )
                continue
            fields: Mapping[str, object] = {str(key): value for key, value in record.items()}
            try:
                invoice = invoice_from_fields(
                    self.source,
                    fields,
                    date_formats=self._date_formats,
                )
            except InvoiceValidationError as exc:
                raw_external_id = record.get("external_id")
                external_id = (
                    str(raw_external_id).strip()
                    if raw_external_id is not None and str(raw_external_id).strip()
                    else None
                )
                self.row_errors.append(
                    ImportRowError(row=ordinal, message=str(exc), external_id=external_id)
                )
                continue
            invoices.append(invoice)
        return invoices

    def _invoices_url(self, since: date | None) -> str:
        base_url = (self._base_url or "").rstrip("/")
        endpoint = self._endpoint.strip().lstrip("/")
        url = f"{base_url}/{endpoint}"
        if since is not None:
            query = urllib.parse.urlencode({"since": since.isoformat()})
            url = f"{url}?{query}"
        return url

    @staticmethod
    def _decode(raw: bytes) -> object:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderApiError("the invoice API returned a non-JSON response") from exc

    @staticmethod
    def _records(payload: object) -> list[object]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("invoices", "data", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise ProviderApiError(
            "the invoice API response must be a JSON list or contain an 'invoices' list"
        )


def build_api_provider(
    settings: Settings | None = None,
    *,
    source: DocumentSource = DocumentSource.IMPORT_INFAKT,
    http_client: HttpClient | None = None,
    validate: bool = True,
) -> ApiProvider:
    """Build the skeleton provider from settings/ENV; validates eagerly by default."""
    resolved = settings or get_settings()
    provider = ApiProvider(
        source=source,
        base_url=resolved.accounting_api_url,
        token=resolved.accounting_api_token,
        endpoint=resolved.accounting_api_invoices_path,
        timeout=resolved.accounting_api_timeout_seconds,
        http_client=http_client,
    )
    if validate:
        provider.validate_configuration()
    return provider
