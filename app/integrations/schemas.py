"""Schema registry and optional XSD validation.

Two responsibilities share this module:

* **Registry** - ``config/schemas.yaml`` is the single source of truth for the KSeF FA(3)
  *format metadata* (namespace, ``KodFormularza`` attributes, wariant) and for the
  download location/checksum of each official XSD. Nothing here (or in the builder) keeps
  the FA(3) namespace or schema version as a literal, so bumping the format is a config
  change, not a code change.
* **Validation** - :func:`validate` compiles an XSD with ``lxml`` and returns a list of
  :class:`Violation` objects. A missing schema file is *not* an error: :func:`check`
  reports it as :attr:`ValidationStatus.SKIPPED` so that invoice export keeps working on
  a machine that never ran ``scripts/fetch_schemas.py``.

The XML parser used here never resolves external entities and never touches the network:
imported XSDs are resolved by file name from the schema's own directory (see
:func:`_compile`), which is how ``FA3.xsd`` finds the shared eD definition files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Final

import yaml
from lxml import etree

__all__ = [
    "CONFIG_PATH",
    "FA3_KEY",
    "PROJECT_ROOT",
    "RESOURCES_DIR",
    "Fa3Format",
    "SchemaConfigError",
    "SchemaError",
    "SchemaImport",
    "SchemaSpec",
    "SchemaValidation",
    "ValidationStatus",
    "Violation",
    "check",
    "clear_cache",
    "fa3_format",
    "get",
    "load_config",
    "validate",
]

#: Repository root; configured ``path`` values are resolved against it.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The schema registry itself.
CONFIG_PATH: Final[Path] = PROJECT_ROOT / "config" / "schemas.yaml"

#: Default download directory (``resources`` is gitignored - schemas are fetched).
RESOURCES_DIR: Final[Path] = PROJECT_ROOT / "resources"

#: Key of the invoice schema used by :mod:`app.integrations.ksef.fa3_builder`.
FA3_KEY: Final[str] = "ksef_fa3"

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "url",
        "sha256",
        "path",
        "optional",
        "namespace",
        "kod_formularza",
        "kod_systemowy",
        "wariant_formularza",
        "wersja_schemy",
        "imports",
        "notes",
    }
)
_ALLOWED_IMPORT_FIELDS: Final[frozenset[str]] = frozenset({"url", "sha256", "filename"})

#: Format metadata the FA(3) builder needs (see :func:`fa3_format`).
_FA3_REQUIRED: Final[tuple[str, ...]] = (
    "version",
    "namespace",
    "kod_formularza",
    "kod_systemowy",
    "wariant_formularza",
    "wersja_schemy",
)


class SchemaConfigError(RuntimeError):
    """``config/schemas.yaml`` is missing, malformed or does not describe what is needed."""


class SchemaError(RuntimeError):
    """A schema exists but cannot be used (unreadable, malformed, unresolved import)."""


class ValidationStatus(StrEnum):
    """Outcome of an XSD validation attempt.

    ``SKIPPED`` means "no schema was available"; it is a normal state, not a failure of
    the document.
    """

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Violation:
    """One machine-readable validation finding.

    ``path`` is the XML location for XSD findings (``line 12``) or an element path for
    structural findings (``Fa/P_15``); the API turns both into the same JSON shape.
    """

    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        """JSON-ready representation used in the ``422`` payload."""
        return {"path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class SchemaImport:
    """An XSD imported/included by the main schema, downloaded next to it."""

    url: str
    sha256: str
    filename: str


@dataclass(frozen=True, slots=True)
class SchemaSpec:
    """One entry of ``config/schemas.yaml``."""

    key: str
    path: Path
    url: str | None = None
    sha256: str | None = None
    version: str | None = None
    optional: bool = False
    namespace: str | None = None
    kod_formularza: str | None = None
    kod_systemowy: str | None = None
    wariant_formularza: int | None = None
    wersja_schemy: str | None = None
    imports: tuple[SchemaImport, ...] = ()
    notes: str | None = None

    @property
    def available(self) -> bool:
        """Whether the XSD file is present on disk (i.e. validation is possible)."""
        return self.path.is_file()


@dataclass(frozen=True, slots=True)
class Fa3Format:
    """Format metadata of the FA(3) logical structure, read from the schema registry."""

    version: str
    namespace: str
    kod_formularza: str
    kod_systemowy: str
    wariant_formularza: int
    wersja_schemy: str


@dataclass(frozen=True, slots=True)
class SchemaValidation:
    """Result of :func:`check`: the status plus every XSD violation."""

    status: ValidationStatus
    violations: tuple[Violation, ...] = ()

    @property
    def passed(self) -> bool:
        """``True`` when the document is valid *or* validation was skipped."""
        return self.status is not ValidationStatus.FAILED

    def as_dict(self) -> dict[str, object]:
        """JSON-ready representation (API headers and error payloads)."""
        return {
            "status": self.status.value,
            "violations": [violation.as_dict() for violation in self.violations],
        }


def _imports(raw: object, *, key: str) -> tuple[SchemaImport, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SchemaConfigError(f"{key}.imports must be a list")
    entries: list[SchemaImport] = []
    for position, item in enumerate(raw):
        where = f"{key}.imports[{position}]"
        if not isinstance(item, dict):
            raise SchemaConfigError(f"{where} must be a mapping")
        unknown = set(item) - _ALLOWED_IMPORT_FIELDS
        if unknown:
            raise SchemaConfigError(f"{where} has unknown fields: {sorted(unknown)}")
        url = item.get("url")
        digest = item.get("sha256")
        if not isinstance(url, str) or not url:
            raise SchemaConfigError(f"{where}.url is required")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise SchemaConfigError(f"{where}.sha256 must be 64 lowercase hex characters")
        filename = item.get("filename") or PurePosixPath(url.split("?")[0]).name
        if not isinstance(filename, str) or not filename:
            raise SchemaConfigError(f"{where}.filename is required")
        entries.append(SchemaImport(url=url, sha256=digest, filename=filename))
    return tuple(entries)


def _spec(key: str, raw: object) -> SchemaSpec:
    if not isinstance(raw, dict):
        raise SchemaConfigError(f"{key} must be a mapping")
    unknown = set(raw) - _ALLOWED_FIELDS
    if unknown:
        raise SchemaConfigError(f"{key} has unknown fields: {sorted(unknown)}")

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise SchemaConfigError(f"{key}.path is required")
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved

    url = raw.get("url")
    if url is not None and (not isinstance(url, str) or not url):
        raise SchemaConfigError(f"{key}.url must be a non-empty string or null")
    digest = raw.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)):
        raise SchemaConfigError(f"{key}.sha256 must be 64 lowercase hex characters or null")
    optional = bool(raw.get("optional", False))
    if url is None and not optional:
        raise SchemaConfigError(f"{key}.url is required unless the entry is optional: true")
    if url is not None and digest is None:
        raise SchemaConfigError(f"{key}.sha256 is required when url is set")

    version = raw.get("version")
    if version is not None and (not isinstance(version, str) or not version):
        raise SchemaConfigError(f"{key}.version must be a non-empty string or null")

    variant = raw.get("wariant_formularza")
    if variant is not None and (not isinstance(variant, int) or isinstance(variant, bool)):
        raise SchemaConfigError(f"{key}.wariant_formularza must be an integer or null")

    notes = raw.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise SchemaConfigError(f"{key}.notes must be a string")

    strings = {
        "namespace": raw.get("namespace"),
        "kod_formularza": raw.get("kod_formularza"),
        "kod_systemowy": raw.get("kod_systemowy"),
        "wersja_schemy": raw.get("wersja_schemy"),
    }
    for field_name, value in strings.items():
        if value is not None and (not isinstance(value, str) or not value):
            raise SchemaConfigError(f"{key}.{field_name} must be a non-empty string or null")

    return SchemaSpec(
        key=key,
        path=resolved,
        url=url,
        sha256=digest,
        version=version,
        optional=optional,
        namespace=strings["namespace"],
        kod_formularza=strings["kod_formularza"],
        kod_systemowy=strings["kod_systemowy"],
        wariant_formularza=variant,
        wersja_schemy=strings["wersja_schemy"],
        imports=_imports(raw.get("imports"), key=key),
        notes=notes,
    )


@lru_cache(maxsize=8)
def _load_config(config_path: Path) -> dict[str, SchemaSpec]:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaConfigError(f"{config_path}: cannot be read: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SchemaConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise SchemaConfigError(f"{config_path}: the document must be a mapping of schemas")
    specs: dict[str, SchemaSpec] = {}
    for key, raw in document.items():
        if not isinstance(key, str) or not key:
            raise SchemaConfigError(f"{config_path}: schema keys must be non-empty strings")
        specs[key] = _spec(key, raw)
    return specs


def load_config(*, config_path: Path | None = None) -> dict[str, SchemaSpec]:
    """Return every configured schema, keyed by name (cached per file path)."""
    return dict(_load_config((config_path or CONFIG_PATH).resolve()))


def clear_cache() -> None:
    """Forget parsed configuration and compiled XSDs (call after editing a schema)."""
    _load_config.cache_clear()
    _compile.cache_clear()


def get(key: str, *, config_path: Path | None = None) -> SchemaSpec:
    """Look one schema up; raises :class:`SchemaConfigError` when it is not configured."""
    specs = _load_config((config_path or CONFIG_PATH).resolve())
    try:
        return specs[key]
    except KeyError as exc:
        raise SchemaConfigError(f"no schema named {key!r} in the registry") from exc


def fa3_format(*, config_path: Path | None = None) -> Fa3Format:
    """FA(3) format metadata from the registry (namespace, KodFormularza, wariant, version)."""
    spec = get(FA3_KEY, config_path=config_path)
    missing = [name for name in _FA3_REQUIRED if getattr(spec, name) is None]
    if missing:
        raise SchemaConfigError(f"{FA3_KEY}: missing FA(3) format metadata: {', '.join(missing)}")
    version = spec.version
    namespace = spec.namespace
    kod_formularza = spec.kod_formularza
    kod_systemowy = spec.kod_systemowy
    wariant_formularza = spec.wariant_formularza
    wersja_schemy = spec.wersja_schemy
    assert version is not None and namespace is not None  # guaranteed by the check above
    assert kod_formularza is not None and kod_systemowy is not None
    assert wariant_formularza is not None and wersja_schemy is not None
    return Fa3Format(
        version=version,
        namespace=namespace,
        kod_formularza=kod_formularza,
        kod_systemowy=kod_systemowy,
        wariant_formularza=wariant_formularza,
        wersja_schemy=wersja_schemy,
    )


def _existing(schema_path: Path | str | None) -> Path | None:
    """Resolve ``schema_path`` to an existing file, or ``None`` when nothing is available."""
    if schema_path is None:
        return None
    path = Path(schema_path)
    return path if path.is_file() else None


class _LocalResolver(etree.Resolver):  # type: ignore[misc]  # lxml has no type stubs
    """Resolve XSD imports by file name from the importing schema's own directory."""

    def __init__(self, directory: Path, schema_name: str) -> None:
        super().__init__()
        self._directory = directory
        self._schema_name = schema_name

    def resolve(self, url: str, pubid: str | None, context: object) -> object:
        candidate = self._directory / PurePosixPath(url.split("?")[0]).name
        if candidate.is_file():
            return self.resolve_filename(str(candidate), context)
        raise SchemaError(
            f"{self._schema_name} references {url!r}, which is not available next to it; "
            "run scripts/fetch_schemas.py to download the configured imports"
        )


@lru_cache(maxsize=8)
def _compile(resolved_path: str, _mtime: float) -> etree.XMLSchema:
    """Compile an XSD (cached by path + mtime); raises :class:`SchemaError` when unusable."""
    path = Path(resolved_path)
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    parser.resolvers.add(_LocalResolver(path.parent, path.name))
    try:
        return etree.XMLSchema(etree.parse(str(path), parser))
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError, OSError) as exc:
        raise SchemaError(f"{path} could not be compiled: {exc}") from exc


def _schema(schema_path: Path) -> etree.XMLSchema:
    return _compile(str(schema_path), schema_path.stat().st_mtime)


def _parser() -> etree.XMLParser:
    """Parser for the *document*: no entity expansion, no network, no DTD loading."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


def validate(xml_bytes: bytes, schema_path: Path | str | None) -> list[Violation]:
    """Validate ``xml_bytes`` against ``schema_path``; return every XSD violation.

    A ``None`` or non-existent path yields an empty list: the caller decides whether that
    means ``SKIPPED`` (see :func:`check`). Malformed XML is reported as a violation rather
    than raised, so an API caller never sees a 500 for a user-supplied document. A schema
    that cannot be compiled raises :class:`SchemaError`, because that is an operator error.
    """
    path = _existing(schema_path)
    if path is None:
        return []
    schema = _schema(path)
    try:
        document = etree.fromstring(xml_bytes, parser=_parser())
    except etree.XMLSyntaxError as exc:
        return [Violation(path="document", message=f"not well-formed XML: {exc}")]
    if schema.validate(document):
        return []
    return [
        Violation(path=f"line {entry.line}", message=entry.message) for entry in schema.error_log
    ]


def check(xml_bytes: bytes, schema_path: Path | str | None) -> SchemaValidation:
    """Like :func:`validate`, but also reports ``SKIPPED`` when no schema is present."""
    path = _existing(schema_path)
    if path is None:
        return SchemaValidation(status=ValidationStatus.SKIPPED)
    violations = tuple(validate(xml_bytes, path))
    status = ValidationStatus.FAILED if violations else ValidationStatus.OK
    return SchemaValidation(status=status, violations=violations)
