#!/usr/bin/env python
"""Download and verify the XSD schemas declared in ``config/schemas.yaml``.

    python scripts/fetch_schemas.py                 # every configured target
    python scripts/fetch_schemas.py --only ksef_fa3 # one target
    python scripts/fetch_schemas.py --check         # verify local copies, download nothing
    python scripts/fetch_schemas.py --config other.yaml

Every file is downloaded from the configured ``url``, hashed with SHA-256 and written only
when the digest equals the ``sha256`` recorded in the registry. A mismatch aborts with exit
code ``2`` and leaves any previous copy untouched; a target without a ``url`` (an
``optional`` entry such as ``jpk_pit``) is reported as SKIPPED instead of failing.

The download is byte-for-byte the Ministry of Finance publication - the schema is never
rewritten, so the checksum keeps identifying the exact document being validated against.
XSDs referenced by ``imports`` are stored next to the main schema, which is where
:mod:`app.integrations.schemas` resolves them from during offline validation.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/fetch_schemas.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.integrations import schemas  # noqa: E402
from app.integrations.schemas import SchemaSpec  # noqa: E402

__all__ = [
    "ChecksumMismatch",
    "FetchError",
    "download",
    "fetch",
    "fetch_imports",
    "main",
    "sha256_digest",
    "verify_checksum",
]

#: Some servers reject the default urllib agent.
USER_AGENT = "taxwise-sme/fetch-schemas (schema verification)"

#: Read timeout for a single schema request.
DEFAULT_TIMEOUT = 60.0


class FetchError(RuntimeError):
    """A schema could not be downloaded or stored."""


class ChecksumMismatch(FetchError):
    """The downloaded bytes do not match the SHA-256 recorded in ``config/schemas.yaml``."""


def sha256_digest(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def verify_checksum(data: bytes, expected: str) -> None:
    """Raise :class:`ChecksumMismatch` unless ``data`` hashes to ``expected``."""
    actual = sha256_digest(data)
    if actual != expected:
        raise ChecksumMismatch(
            f"checksum mismatch: expected {expected}, downloaded {actual} - aborting"
        )


def download(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Fetch ``url`` and return its bytes (``file://`` URLs work as well as http/https)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data: bytes = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError(f"{url}: download failed: {exc}") from exc
    if not data:
        raise FetchError(f"{url}: the server returned an empty document")
    return data


def _matches(path: Path, expected: str) -> bool:
    if not path.is_file():
        return False
    return sha256_digest(path.read_bytes()) == expected


def _store(path: Path, data: bytes) -> None:
    """Write ``data`` atomically (temporary file in the same directory, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def fetch(
    url: str,
    expected: str,
    target: Path,
    *,
    downloader: Callable[[str], bytes] | None = None,
) -> bool:
    """Download-verify-store one file; returns ``True`` when the target was written."""
    if _matches(target, expected):
        return False
    data = (downloader or download)(url)
    verify_checksum(data, expected)
    _store(target, data)
    return True


def fetch_imports(
    spec: SchemaSpec, *, downloader: Callable[[str], bytes] | None = None
) -> list[str]:
    """Fetch every XSD ``spec`` imports into the main schema's directory."""
    messages: list[str] = []
    for imported in spec.imports:
        target = spec.path.parent / imported.filename
        written = fetch(imported.url, imported.sha256, target, downloader=downloader)
        messages.append(f"{'fetched' if written else 'up to date'} {target}")
    return messages


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"registry to read (default: {schemas.CONFIG_PATH})",
    )
    parser.add_argument(
        "--only",
        action="append",
        dest="keys",
        metavar="KEY",
        help="fetch only this schema (repeatable); defaults to every configured target",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the local copies only; never download",
    )
    return parser


def _selected(specs: dict[str, SchemaSpec], keys: list[str] | None) -> list[SchemaSpec]:
    if not keys:
        return list(specs.values())
    missing = [key for key in keys if key not in specs]
    if missing:
        raise FetchError(f"unknown schema(s): {', '.join(missing)}")
    return [specs[key] for key in keys]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns a process exit code."""
    args = _parser().parse_args(argv)
    try:
        specs = _selected(schemas.load_config(config_path=args.config), args.keys)
    except (schemas.SchemaConfigError, FetchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    failures = 0
    for spec in specs:
        if spec.url is None or spec.sha256 is None:
            print(f"skipped {spec.key}: no url configured (optional target)")
            continue
        if args.check:
            if _matches(spec.path, spec.sha256):
                print(f"ok {spec.key}: {spec.path}")
            else:
                print(f"error {spec.key}: {spec.path} is missing or has a wrong checksum")
                failures += 1
            continue
        try:
            written = fetch(spec.url, spec.sha256, spec.path)
            print(f"{'fetched' if written else 'up to date'} {spec.key}: {spec.path}")
            for message in fetch_imports(spec):
                print(f"  {message}")
        except ChecksumMismatch as exc:
            print(f"error {spec.key}: {exc}", file=sys.stderr)
            return 2
        except FetchError as exc:
            print(f"error {spec.key}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

