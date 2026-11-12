"""Policy guard: tax rates and thresholds are data, never code.

The acceptance criterion is a ``grep`` with an empty result; this test encodes the same
grep so CI fails the moment someone types a Polish tax rate into the source tree.
Allowed places for numbers: ``tax_year_params.payload`` rows, ``tax_profiles`` columns and
API/user input.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRECTORIES = (PROJECT_ROOT / "app", PROJECT_ROOT / "alembic")

#: label -> regex of a well-known Polish rate/threshold literal that must not be hardcoded.
FORBIDDEN_PATTERNS: dict[str, str] = {
    # Fractions may be written with trailing zeros (0.0500), hence no trailing \b.
    "VAT 23%": r"\b0\.23",
    "VAT 8%": r"\b0\.08",
    "VAT 5% / IP BOX": r"\b0\.05",
    "PIT 19%": r"\b0\.19",
    "PIT/ryczalt 12%": r"\b0\.12",
    "ryczalt 15%": r"\b0\.15",
    "zdrowotna 9%": r"\b0\.09",
    "zdrowotna 9.76%": r"\b9\.76",
    "VAT threshold 200k": r"\b200[ _]?000\b",
    "lump-sum threshold 300k": r"\b300[ _]?000\b",
    "lump-sum threshold 66 660": r"\b66660\b",
    "percent sign rates": r"\b(?:5|8|9|12|15|19|23)\s?%",
}


def _python_files() -> Iterator[Path]:
    for directory in SCANNED_DIRECTORIES:
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_no_rate_or_threshold_literals_in_source() -> None:
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN_PATTERNS.items():
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} -> {label} ({pattern})")
    assert offenders == [], "Hardcoded tax rate/threshold found: " + "; ".join(offenders)


def test_policy_scan_covers_the_source_tree() -> None:
    """Sanity check that the policy test above actually scans a non-empty tree."""
    scanned = list(_python_files())
    assert len(scanned) >= 5
    assert any(path.name == "models.py" for path in scanned)
