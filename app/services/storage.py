"""Content-addressed storage for uploaded documents.

Files live directly under ``DOCUMENTS_DIR`` (the setting/env var of the same name) and
are named after their digest, never after the client-supplied name::

    <DOCUMENTS_DIR>/<sha256><extension>

That gives three properties the intake endpoint relies on:

* **deduplication** - identical bytes always map to the same relative path, so the same
  document cannot be stored twice;
* **atomicity** - bytes are written to a hidden temporary file in the same directory,
  flushed and fsynced, and only then hard-linked into place. Readers never observe a
  partially written file and an existing file is never replaced (``os.link`` fails with
  ``FileExistsError`` instead of clobbering);
* **traversal safety** - the digest is re-validated with a regular expression, extensions
  come from a strict allowlist, and every resolved path must stay inside the storage root.

The client file name is used for classification only. It is sanitised before use and is
never part of a filesystem path, which keeps personal data out of the directory layout.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.settings import DOCUMENTS_DIR, get_settings

__all__ = [
    "ALLOWED_EXTENSIONS",
    "DOCUMENTS_DIR",
    "MAX_FILENAME_LENGTH",
    "SHA256_PATTERN",
    "TEMP_PREFIX",
    "PathTraversalError",
    "StorageError",
    "StoredFile",
    "UnsafeFilenameError",
    "UnsupportedFileTypeError",
    "document_path",
    "documents_root",
    "ensure_within",
    "extension_for",
    "sanitize_filename",
    "save_document",
    "stored_name",
]

#: Extensions accepted by the intake endpoint; anything else is rejected, not renamed.
ALLOWED_EXTENSIONS: Final[frozenset[str]] = frozenset({".pdf", ".xml", ".csv"})

#: Every stored name must be a lowercase hex digest - no separators, dots or dots-dots.
SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

#: Prefix of the temporary file a write goes through before being linked into place.
TEMP_PREFIX: Final[str] = ".upload-"

MAX_FILENAME_LENGTH: Final[int] = 255


class StorageError(Exception):
    """Base class for storage failures; callers translate these to HTTP errors."""


class UnsafeFilenameError(StorageError):
    """The client file name cannot be used safely (empty, control-only, too long)."""


class PathTraversalError(UnsafeFilenameError):
    """The file name or path tries to address something outside the storage root."""


class UnsupportedFileTypeError(StorageError):
    """The extension is not one of :data:`ALLOWED_EXTENSIONS`."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Result of :func:`save_document`; ``created`` is ``False`` for a repeat write."""

    path: Path
    sha256: str
    size_bytes: int
    created: bool

    @property
    def name(self) -> str:
        """Content-addressed file name relative to the storage root."""
        return self.path.name


def documents_root(root: Path | None = None) -> Path:
    """Resolve the storage root: an explicit ``root`` or the ``DOCUMENTS_DIR`` setting."""
    base = root if root is not None else get_settings().documents_dir
    return base.expanduser().resolve()


def ensure_within(path: Path, root: Path) -> Path:
    """Return ``path`` resolved, refusing anything that escapes ``root``."""
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise PathTraversalError("path escapes the documents directory")
    return resolved


def sanitize_filename(filename: str) -> str:
    """Return the bare client file name, rejecting anything path-shaped.

    Only a plain file name is accepted: separators, NUL bytes, dotted names (``.``,
    ``..``, ``.env``) and over-long names are rejected rather than rewritten, so a
    traversal attempt surfaces to the caller instead of being silently masked. Control
    characters are stripped because they have no place in a document name.
    """
    if not filename or "\x00" in filename:
        raise UnsafeFilenameError("file name is empty or contains a NUL byte")
    if "/" in filename or "\\" in filename:
        raise PathTraversalError("file name must not contain path separators")
    if filename in {".", ".."} or filename.startswith("."):
        raise PathTraversalError("file name must not be a relative or hidden name")

    cleaned = "".join(char for char in filename if char.isprintable()).strip()
    if not cleaned:
        raise UnsafeFilenameError("file name contains only control characters")
    if len(cleaned) > MAX_FILENAME_LENGTH:
        raise UnsafeFilenameError("file name is too long")
    return cleaned


def extension_for(filename: str) -> str:
    """Return the lowercased, allowlisted extension of a sanitised file name."""
    extension = Path(sanitize_filename(filename)).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsupportedFileTypeError(
            f"unsupported file extension {extension or '(none)'}; allowed: {allowed}"
        )
    return extension


def stored_name(sha256: str, extension: str) -> str:
    """Return the content-addressed name ``<sha256><extension>``.

    The digest is validated again here so a caller cannot smuggle a path through it.
    """
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    if extension not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(f"unsupported file extension {extension!r}")
    return f"{sha256}{extension}"


def document_path(sha256: str, extension: str, *, root: Path | None = None) -> Path:
    """Absolute path of a stored document, verified to be inside the storage root."""
    resolved_root = documents_root(root)
    return ensure_within(resolved_root / stored_name(sha256, extension), resolved_root)


def save_document(
    content: bytes,
    *,
    sha256: str,
    extension: str,
    root: Path | None = None,
) -> StoredFile:
    """Atomically store ``content`` as ``<root>/<sha256><extension>``.

    The write goes to a temporary file first; ``os.link`` then atomically publishes it
    under the content-addressed name without ever replacing existing bytes. A duplicate
    write is reported with ``created=False`` and leaves the stored file untouched.
    """
    if not content:
        raise StorageError("refusing to store an empty document")

    resolved_root = documents_root(root)
    target = document_path(sha256, extension, root=resolved_root)
    resolved_root.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()

    descriptor, temporary = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=resolved_root)
    temporary_path = Path(temporary)
    created = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        if not existed:
            try:
                os.link(temporary_path, target)
                created = True
            except FileExistsError:
                # Another writer published the same digest first: identical bytes win.
                created = False
            except OSError:  # pragma: no cover - filesystems without hard-link support
                if target.exists():
                    created = False
                else:
                    os.replace(temporary_path, target)
                    created = True
    finally:
        temporary_path.unlink(missing_ok=True)

    return StoredFile(path=target, sha256=sha256, size_bytes=len(content), created=created)
