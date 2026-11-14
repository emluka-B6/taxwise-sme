"""Heuristic classification of uploaded documents.

The intake endpoint cannot trust the client for anything but a hint, so classification is
a deterministic heuristic over two cheap signals:

* the *sanitised file name* - checked first, because ``faktura_2026_03.pdf`` is a strong
  signal and document contents often mention several taxes at once;
* a *bounded prefix of the content* (``CONTENT_SNIFF_BYTES``) - never the whole file, so
  classification stays O(1) in upload size and never buffers a document in memory.

Both signals are matched case- and diacritic-insensitively (``sprzedaż`` == ``sprzedaz``)
and, when both contain several markers, the first matching rule wins. The result is only
a ``DocumentType``; the user corrects a wrong guess through ``PATCH /api/documents/{id}``.

The persisted ``DocumentType`` enum has no generic declaration/JPK member, so the mapping
is documented per rule below rather than by widening a CHECK-constrained column.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final

from app.domain.enums import DocumentType

__all__ = [
    "CONTENT_SNIFF_BYTES",
    "RULES",
    "ClassificationRule",
    "classify",
    "normalise",
]

#: How much of the file is inspected for content rules (uploads are untrusted and may be
#: arbitrarily large, so only a fixed-size prefix is decoded).
CONTENT_SNIFF_BYTES: Final[int] = 64 * 1024

#: Encodings tried for the content prefix, in order: Polish paperwork is usually UTF-8
#: (optionally with a BOM) or Windows-1250.
CONTENT_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "cp1250")

#: ``str.casefold`` leaves Polish ``ł`` untouched (it is not a combining letter).
_POLISH_FOLD: Final[dict[int, str]] = str.maketrans({"ł": "l"})


@dataclass(frozen=True, slots=True)
class ClassificationRule:
    """One ordered heuristic: any needle hit maps the document to ``document_type``."""

    document_type: DocumentType
    needles: tuple[str, ...]
    rationale: str

    def matches(self, haystack: str) -> bool:
        """Return ``True`` when ``haystack`` contains any of this rule's needles."""
        return any(needle in haystack for needle in self.needles)


#: First match wins. Specific statements (ZUS/PIT/JPK) precede generic invoices, and
#: sale/cost qualifiers precede the plain "faktura" fallback.
RULES: Final[tuple[ClassificationRule, ...]] = (
    ClassificationRule(
        DocumentType.ZUS,
        ("zus dra", "zus"),
        "ZUS DRA declaration or contribution statement",
    ),
    ClassificationRule(
        DocumentType.PIT,
        ("pit-",),
        "PIT return or advance (PIT-36, PIT-37, PIT-11, ...)",
    ),
    ClassificationRule(
        DocumentType.VAT_INVOICE_SALES,
        ("jpk",),
        "JPK VAT ledger; DocumentType has no JPK member, the sales register is closest",
    ),
    ClassificationRule(
        DocumentType.VAT_INVOICE_SALES,
        ("faktura sprzeda", "sprzedaz", "sales invoice"),
        "sales invoice (explicit sale marker)",
    ),
    ClassificationRule(
        DocumentType.VAT_INVOICE_COST,
        ("faktura koszt", "koszt", "zakup", "cost invoice"),
        "cost invoice (explicit purchase marker)",
    ),
    ClassificationRule(
        DocumentType.VAT_INVOICE_COST,
        ("faktura", "fa(", "invoice"),
        "invoice without a sale/purchase qualifier; uploads are assumed to be costs",
    ),
    ClassificationRule(
        DocumentType.PIT,
        ("deklaracja",),
        "declaration without a tax-specific prefix; the annual PIT return is the default",
    ),
)


def normalise(text: str) -> str:
    """Lowercase ``text`` and strip Polish diacritics for needle matching."""
    folded = unicodedata.normalize("NFKD", text.casefold()).translate(_POLISH_FOLD)
    return "".join(char for char in folded if not unicodedata.combining(char))


def _decode_prefix(content: bytes) -> str:
    """Decode the inspected prefix leniently; never raises on binary payloads."""
    sample = content[:CONTENT_SNIFF_BYTES]
    for encoding in CONTENT_ENCODINGS:
        try:
            return sample.decode(encoding)
        except UnicodeDecodeError:
            continue
    return sample.decode("utf-8", errors="ignore")


def classify(filename: str, content: bytes = b"") -> DocumentType:
    """Classify a document by its file name, falling back to a prefix of its content.

    Returns ``DocumentType.OTHER`` when neither signal matches a rule. The function is
    pure and side-effect free: nothing is logged, so document contents cannot leak.
    """
    name_haystack = normalise(filename)
    for rule in RULES:
        if rule.matches(name_haystack):
            return rule.document_type

    content_haystack = normalise(_decode_prefix(content))
    for rule in RULES:
        if rule.matches(content_haystack):
            return rule.document_type

    return DocumentType.OTHER
