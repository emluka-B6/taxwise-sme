#!/usr/bin/env python
"""Seed ``tax_year_params`` (and, when a profile exists, the deadline calendar).

    python scripts/seed_tax_params.py                 # every config/tax_params/*.yaml
    python scripts/seed_tax_params.py --year 2026     # one year
    python scripts/seed_tax_params.py --no-deadlines  # parameters only

The script is idempotent: parameters are upserted by ``(year, version)`` and deadlines by
``(kind, period, due_date)``, so running it twice leaves the database unchanged.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/seed_tax_params.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.exc import OperationalError, SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.session import get_sessionmaker  # noqa: E402
from app.services import deadlines as deadlines_service  # noqa: E402
from app.services import tax_params  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        dest="years",
        metavar="YYYY",
        help="Seed only this year (repeatable). Defaults to every shipped file.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help=f"Directory with <year>.yaml files (default: {tax_params.CONFIG_DIR}).",
    )
    parser.add_argument(
        "--no-deadlines",
        action="store_true",
        help="Only seed parameters; do not generate the deadline calendar.",
    )
    return parser


def _config_files(years: list[int] | None, config_dir: Path | None) -> list[Path]:
    if years:
        return [tax_params.config_path(year, config_dir=config_dir) for year in years]
    files = tax_params.iter_config_files(config_dir=config_dir)
    if not files:
        raise SystemExit(
            f"no *.yaml tax parameter files in {config_dir or tax_params.CONFIG_DIR}"
        )
    return list(files)


def _seed_deadlines(session: Session, year: int) -> str:
    """Generate the calendar for ``year`` when a profile covers it."""
    profile = deadlines_service.active_profile(session, date(year, 12, 31))
    if profile is None:
        return "no tax profile covers this year, deadlines skipped"
    result = deadlines_service.generate_deadlines(session, year, profile=profile)
    return f"deadlines: {result.created} created, {result.existing} already present"


def seed(years: list[int] | None, *, config_dir: Path | None, with_deadlines: bool) -> int:
    """Seed the database; returns a process exit code."""
    try:
        files = _config_files(years, config_dir)
    except (tax_params.TaxParamsError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with get_sessionmaker()() as session:
        for path in files:
            try:
                params = tax_params.load_file(path)
            except tax_params.TaxParamsError as exc:
                session.rollback()
                print(f"error: {path}: {exc}", file=sys.stderr)
                return 2
            row, created = tax_params.upsert(session, params)
            action = "created" if created else "updated"
            details = "" if not with_deadlines else f", {_seed_deadlines(session, params.year)}"
            session.commit()
            print(
                f"{action} {row.year} v{row.version}: {row.source_url} "
                f"(effective_from={row.effective_from.isoformat()}{details})"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _parser().parse_args(argv)
    try:
        return seed(args.years, config_dir=args.config_dir, with_deadlines=not args.no_deadlines)
    except OperationalError:
        print(
            "error: the database schema is missing - run `alembic upgrade head` first",
            file=sys.stderr,
        )
        return 1
    except SQLAlchemyError as exc:
        print(f"error: database failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
