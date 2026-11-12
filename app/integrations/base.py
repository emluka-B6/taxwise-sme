"""Protocol implemented by invoice-platform importers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.domain.enums import DocumentSource

__all__ = ["InvoiceImporter"]


@runtime_checkable
class InvoiceImporter(Protocol):
    """Common surface of an invoice platform adapter.

    Implementations map platform payloads onto ``Document`` rows, keeping
    ``external_id`` stable so that ``unique(source, external_id)`` guarantees idempotency.
    """

    source: DocumentSource

    def list_document_refs(self, period: str) -> Sequence[str]:
        """Return external ids of documents available for ``period`` (e.g. ``2026-03``)."""
        ...

    def download(self, external_id: str, target_dir: Path) -> Path:
        """Download a document and return the local file path."""
        ...
