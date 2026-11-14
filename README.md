# TaxWise SME

Skeleton of a finance assistant for Polish sole proprietorships (JDG, B2B): document
intake, tax profile, deadlines, an auditable decision log and a citation-backed knowledge
base. This repository contains the **data model, migrations, API bootstrap and CI** — the
business logic (tax computation, importers, retrieval) is intentionally left as typed
skeletons.

## Stack

| Concern | Choice |
| --- | --- |
| Runtime | Python 3.12 |
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.0 typed (`Mapped` / `mapped_column`) |
| Migrations | Alembic (autogenerate + manual review) |
| Validation / settings | Pydantic v2, pydantic-settings |
| DB | SQLite locally, PostgreSQL in production (`DATABASE_URL`) |
| Quality | pytest, ruff, mypy (strict) |

## Quickstart

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # add ",postgres" to use PostgreSQL locally

cp .env.example .env             # optional; defaults to SQLite in ./taxwise.db
alembic upgrade head             # creates all tables
uvicorn app.main:app --reload    # http://127.0.0.1:8000/health
curl -s localhost:8000/health
```

Using PostgreSQL instead of SQLite is a one-line change:

```bash
export DATABASE_URL="postgresql+psycopg://user:password@localhost:5432/taxwise"
alembic upgrade head
```

## Project layout

```
app/
  main.py            # FastAPI app factory + ASGI entrypoint
  settings.py        # pydantic-settings (DATABASE_URL, ENVIRONMENT, ...)
  api/               # routers: health, tax_params, deadlines, decisions
  domain/            # pure enums, no infrastructure imports
  db/
    base.py          # DeclarativeBase + deterministic naming convention
    types.py         # JSONType, EmbeddingVector, sa_enum (cross-dialect)
    models.py        # all ORM models
    session.py       # lazy engine/session factories, FastAPI dependency
  services/          # tax params loader, deadline calendar, ingestion
  integrations/      # inFakt / Fakturownia / wFirma adapters (Protocol only)
  assistant/         # retrieval-augmented answers, citations (Protocol only)
config/
  tax_params/        # 2025.yaml, 2026.yaml - legal parameters as data
scripts/
  seed_tax_params.py # idempotent loader: YAML -> tax_year_params
alembic/
  env.py             # URL from DATABASE_URL, batch mode on SQLite
  versions/0001_initial.py
  versions/0002_deadline_statuses_and_unique_key.py
tests/               # schema, API, migration and policy tests
```

## Configuration

All settings are environment variables (or `.env`), parsed by `app.settings.Settings`:

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+pysqlite:///./taxwise.db` | Only `sqlite` and `postgresql` schemes are accepted |
| `ENVIRONMENT` | `local` | `local`, `test`, `staging`, `prod` |
| `DEBUG` | `false` | Passed to FastAPI |
| `SQL_ECHO` | `false` | SQLAlchemy statement logging |

Tax rates and thresholds are **not** settings and **not** constants: they are rows in
`tax_year_params` (versioned, source-linked) and per-taxpayer values in `tax_profiles`.

## Tax parameters (`config/tax_params/<year>.yaml`)

Every legal figure the assistant needs lives in a per-year YAML document: PIT scale,
tax-free amount, flat and lump-sum rates, IP BOX rate, VAT exemption threshold and rates,
ZUS components, and the filing calendar. Each document carries `source_url` (a specific
official page), `effective_from`, `version` and `notes`, so a figure can always be traced
back to the act or announcement it came from.

```bash
python scripts/seed_tax_params.py            # load every file (idempotent)
python scripts/seed_tax_params.py --year 2026 --no-deadlines
curl -s localhost:8000/api/tax-params/2026  # source_url + effective_from + payload
```

`app/services/tax_params.py` is the only reader:

* `load(year)` → a frozen Pydantic model (cached per year, `clear_cache()` invalidates);
* validation: scale thresholds strictly increasing (only the last bracket may be open),
  rates are positive fractions, VAT rates distinct and ascending, `zus.components`
  (base × rate) must add up to `zus.total_full_pln` within a cent, `effective_from` must
  fall in the file's year, unknown keys are rejected, `source_url` must be a specific
  document (not a site root), and each `deadlines[].rule` must match its `kind`;
* `upsert(session, params)` → idempotent write keyed by `UNIQUE(year, version)`;
* the payload is stored as JSON with decimals serialised as exact strings.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/tax-params/{year}` | Stored, validated document of the highest version (404 when the year was never seeded) |

## Deadlines and decisions

`app/services/deadlines.py` turns `<year>.yaml → deadlines[]` plus the active
`TaxProfile` into `deadlines` rows. A rule never hardcodes a date: the due date is
`period end + offset_days`, so changing a filing date is a YAML edit.

| Rule | Obligation | Periods generated |
| --- | --- | --- |
| `vat_monthly` | VAT declaration and payment (VAT-registered, monthly) | 12 months |
| `vat_quarterly` | VAT declaration and payment (quarterly filer) | 4 quarters |
| `zus_dra` | ZUS DRA declaration and contributions | 12 months |
| `pit_advance_monthly` | PIT advance | 12 months |
| `pit_annual` | Annual PIT return/payment | the tax year (due next year) |

Generation is idempotent (`UNIQUE(kind, period, due_date)`), never overwrites an existing
row's status or notes, and reports `created`/`existing`. Statuses are `OPEN`, `DONE` and
`OVERDUE`, where `OVERDUE` is *derived*: an `OPEN` deadline whose due date is before today
in **Europe/Warsaw**. Legacy stored values (`PENDING`, `MISSED`, `WAIVED`) are normalised on
read and rejected on write.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/deadlines?from=&to=&status=` | Deadlines in due-date order, filtered by range and derived status |
| `PATCH /api/deadlines/{id}` | Mark `OPEN`/`DONE`/`OVERDUE` and/or add a note |
| `POST /api/deadlines/generate` | `{"year": 2026, "vat_cadence": "monthly"\|"quarterly"}` - idempotent generation for the active profile (409 without one) |
| `POST /api/decisions` | Append an auditable decision with `source_refs` (list of URLs); 409 on a duplicate `(topic, decided_at)` |
| `GET /api/decisions?topic=&limit=` | Decisions newest first |

VAT cadence is passed to the generator rather than stored on `tax_profiles` (the profile
schema carries no cadence column yet), so monthly and quarterly filers are both modelled
without a schema change.


## Data model

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `documents` | Invoices, ZUS/PIT statements, other paperwork + file metadata | `UNIQUE(source, external_id)` for importer idempotency, index on `sha256` (dedupe by content), index on `period` |
| `tax_profiles` | Versioned taxpayer setup: PIT regime, VAT status, ZUS variant, IP BOX rate | `CHECK(valid_to IS NULL OR valid_to >= valid_from)` |
| `deadlines` | Filing/payment obligations and their status | `UNIQUE(kind, period, due_date)` (idempotent generation), indexes on `due_date`, `period` |
| `decisions` | Append-only audit trail of tax decisions | `UNIQUE(topic, decided_at)` |
| `knowledge_sources` | Official publications (MF, ZUS, ISAP) with content hash + version | index on `sha256` |
| `knowledge_chunks` | Retrieval chunks; embedding is JSON **text** | `UNIQUE(source_id, ordinal)`, FK `ON DELETE CASCADE` |
| `assistant_answers` | Question/answer pairs with citations and `disclaimer_shown` | — |
| `tax_year_params` | Rates/thresholds/bases as JSON data with an official `source_url` | `UNIQUE(year, version)` |

Money is always `NUMERIC(14, 2)` (`Decimal` in Python) — `tests/test_models.py` asserts it.
`period` is a string such as `2026-03` (month) or `2026-Q1` (quarter).

## Cross-database strategy

The same schema must run on SQLite (local, tests) and PostgreSQL (production). All
differences are isolated in `app/db/types.py`:

* `JSONType = JSON().with_variant(JSONB(), "postgresql")` — JSON on SQLite, JSONB on PG.
* `EmbeddingVector` — `TypeDecorator` over `TEXT` that stores `[0.1, -0.2, ...]` as JSON.
  **No pgvector** dependency; swapping in a native vector type later is one migration.
* `sa_enum(...)` — `VARCHAR(32)` + `CHECK` constraint instead of native PG `ENUM`, so
  migrations never need `CREATE TYPE`/`DROP TYPE` bookkeeping.
* Deterministic constraint naming (`app/db/base.py`) keeps autogenerate diffs stable and
  makes constraints droppable by name on both dialects (SQLite uses batch mode).

## No hardcoded rates

A test (`tests/test_no_hardcoded_rates.py`) plus the grep below must both stay empty:

```bash
grep -rnE '0\.(05|08|09|12|15|19|23)|200[ _]?000|300[ _]?000|66660|9\.76' app alembic
```

If a rate is needed, add a `tax_year_params` row (with `source_url` and `effective_from`)
and read it through a service.

## Migrations

```bash
alembic upgrade head            # apply
alembic check                   # fail if models and migrations drift
alembic revision --autogenerate -m "add xyz"   # then REVIEW the generated file
alembic downgrade -1            # roll back one revision
```

`alembic/env.py` reads `DATABASE_URL` through `app.settings`, enables `compare_type=True`
and `render_as_batch=True` for SQLite (needed for future `ALTER TABLE`s). `0001_initial.py`
is a hand-reviewed revision; `alembic check` runs the autogenerate comparator and confirms
that an autogenerate run yields an empty diff on both dialects. Inside it, enum values are
frozen literals so future Python enum edits cannot silently rewrite history, and the
cross-dialect types are imported from `app.db.types` so JSONB/TEXT parity is guaranteed.

## Tests

```bash
pytest -q
```

Coverage highlights:

* `tests/test_models.py` — `JSONType` compiles to `JSONB` on PostgreSQL and `JSON` on
  SQLite; every table compiles on both dialects; money is `NUMERIC(14, 2)`; JSON and
  embedding round-trips on SQLite; uniqueness of `(source, external_id)`,
  `(year, version)`, `(topic, decided_at)`; status/currency/disclaimer defaults;
  `sha256` index exists; `tax_profiles` carries no baked-in rates.
* `tests/test_migrations.py` — `alembic upgrade head` on a clean SQLite file, `alembic
  check` (no drift), `downgrade base` reversibility, and offline PostgreSQL DDL rendering.
* `tests/test_health.py` — `GET /health` → 200 `{"status": "ok", ...}`, OpenAPI, settings
  validation.
* `tests/test_tax_params.py` — 2025/2026 load, invalid files (ZUS total, thresholds,
  negative rate, typo key, wrong `effective_from`, rule/kind mismatch) are rejected,
  seeding is idempotent, `(year, version)` versioning, the endpoint returns `source_url` +
  `effective_from` and 404s for a missing year.
* `tests/test_deadlines.py` — generation for a VAT+scale profile (12 VAT + 12 ZUS +
  12 advances + 1 annual, due dates from the YAML offsets), idempotency, quarterly VAT,
  VAT-exempt profiles, `OVERDUE` after the due date in Europe/Warsaw, list filters,
  `PATCH`, `POST /generate`, and decision CRUD (URL validation, 409 on duplicates).
* `tests/test_no_hardcoded_rates.py` — the policy grep above.

## CI

`.github/workflows/ci.yml` runs on Python 3.12 with a PostgreSQL 16 service:

1. `ruff check .`
2. `mypy app`
3. `pytest -q`
4. `alembic upgrade head` + `alembic check` on a **clean SQLite** database
5. `alembic upgrade head` + `alembic check` + `downgrade base` + `upgrade head` on a
   **clean PostgreSQL** database

## Assumptions and limitations

* `amount_gross` is mandatory; `amount_net` / `vat_amount` are nullable because ZUS/PIT
  documents have no VAT split. Amounts are stored in `currency` (default `PLN`) with no FX
  conversion yet.
* `sha256` / `file_path` are nullable because `MANUAL` documents may have no file. The
  `sha256` index supports content dedupe; `UNIQUE(source, external_id)` covers importer
  idempotency (NULL `external_id` rows are treated as distinct by both dialects).
* `period` is free-form text (`2026-03`, `2026-Q1`); format validation belongs to the API
  layer, not to the DB.
* `deadline_kind` / `deadline_status` / all other enum members are the minimum agreed set
  for this skeleton; adding values requires a data migration because enums are CHECK
  constrained (see `0002`, which adds `PIT_ANNUAL`, `OPEN` and `OVERDUE`).
* `OVERDUE` is derived, not stored: nothing marks a deadline late in the background, so
  the value is computed per request from the Warsaw date. `PENDING`/`MISSED`/`WAIVED` are
  legacy members kept only so old rows still load.
* `generate_deadlines` models obligations whose *period* falls inside the requested year,
  so the annual PIT return for 2025 is due in 2026 and appears in the calendar of 2025.
* VAT settlement cadence (monthly vs quarterly) is an argument of the generator, not a
  `tax_profiles` column; adding it to the profile is a one-column migration.
* ZUS figures in the YAML are planning values for the "full" variant (including the
  minimum health contribution); the actual health contribution depends on income and is
  computed elsewhere. Always re-check the values against `source_url`.
* `tax_profiles` validity ranges may overlap (no exclusion constraint, SQLite does not
  support one); services must select the row with the latest `valid_from` covering a date.
* Embeddings are JSON text and similarity search is expected to run in Python for now;
  replacing `EmbeddingVector` with a native vector type is a contained migration.
* `/health` is a liveness probe without a database check by design; a separate readiness
  probe should query the DB when deployment needs it.
* No authentication/authorisation layer is included in the skeleton.

## Verification

```bash
alembic upgrade head && python scripts/seed_tax_params.py \
  && pytest -q && ruff check . && mypy app \
  && curl -s localhost:8000/api/tax-params/2026 && curl -s localhost:8000/health
```

Expected: migration applies, the seed script reports created/updated rows, all tests pass,
linters clean, `/api/tax-params/2026` returns the stored document with `source_url` and
`effective_from`, and `/health` returns
`{"status":"ok","app":"TaxWise SME API","version":"0.1.0","environment":"local"}`.

