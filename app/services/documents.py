"""Document ingestion helpers.

Hashing is the dedupe primitive of the ingestion pipeline: the ``documents.sha256`` index
lets the importer skip files that were already stored, regardless of source.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["HASH_READ_SIZE", "file_sha256"]

#: Read size for streaming hashes; keeps memory flat for large PDFs.
HASH_READ_SIZE = 1024 * 1024


def file_sha256(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_READ_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
