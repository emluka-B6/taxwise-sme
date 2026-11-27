"""Shared, dependency-free helpers for the XML integrations (JPK_PIT and FA(3)).

Money rounding, timestamp/date/decimal parsing, namespace-qualified names and a common
structural-checker base are identical across the builders and validators. Keeping them in
one place prevents the three modules from drifting apart.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from lxml import etree

from app.domain.money import money
from app.integrations.schemas import Violation

__all__ = [
    "STAMP_FORMAT",
    "XmlChecker",
    "money",
    "money_text",
    "parse_date",
    "parse_decimal",
    "qname",
    "stamp",
]

#: ``DataWytworzenia`` format: UTC, second precision, trailing ``Z`` (``xs:dateTime``).
STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"


def money_text(value: Decimal) -> str:
    """Render ``value`` as a two-decimal money string (``ROUND_HALF_UP``)."""
    return f"{money(value):.2f}"


def stamp(value: datetime) -> str:
    """Render ``value`` as UTC with second precision and a trailing ``Z``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).strftime(STAMP_FORMAT)


def parse_date(text: str | None) -> date | None:
    """Parse an ISO-8601 date, or ``None`` when absent or malformed."""
    if text is None:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def parse_decimal(text: str | None, *, pattern: re.Pattern[str] | None = None) -> Decimal | None:
    """Parse a finite decimal, or ``None`` when absent, malformed or non-finite.

    ``pattern`` (when given) must match the whole trimmed value; the JPK builder passes
    its amount pattern here to reject exponent notation and more than two fraction digits.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if pattern is not None and pattern.fullmatch(stripped) is None:
        return None
    try:
        value = Decimal(stripped)
    except InvalidOperation:  # pragma: no cover - a pattern (when given) already filters
        return None
    return value if value.is_finite() else None


def qname(namespace: str, name: str) -> str:
    """Qualified element name in ``namespace``."""
    return f"{{{namespace}}}{name}"


class XmlChecker:
    """Base for structural XML checkers: namespace plumbing and violation accumulation."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.violations: list[Violation] = []

    def add(self, path: str, message: str) -> None:
        self.violations.append(Violation(path=path, message=message))

    def qname(self, name: str) -> str:
        return qname(self.namespace, name)

    def find(self, parent: etree._Element, name: str) -> etree._Element | None:
        return parent.find(self.qname(name))

    def text(self, parent: etree._Element, name: str) -> str | None:
        element = self.find(parent, name)
        if element is None or element.text is None:
            return None
        return str(element.text).strip()

    def require(self, parent: etree._Element, name: str, path: str) -> str | None:
        value = self.text(parent, name)
        if not value:
            self.add(f"{path}/{name}", "required element is missing or empty")
            return None
        return value
