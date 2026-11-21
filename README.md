# TaxWise SME

Skeleton of a finance assistant for Polish sole proprietorships (JDG, B2B): document
intake, tax profile, deadlines, an auditable decision log, a citation-backed knowledge base
and a deterministic tax/contribution calculation engine. This repository contains the
**data model, migrations, API bootstrap, calculation formulas and CI** — importers and
retrieval are intentionally left as typed skeletons.

## Stack

| Concern | Choice |
| --- | --- |
| Runtime | Python 3.12 |
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.0 typed (`Mapped` / `mapped_column`) |
| Migrations | Alembic (autogenerate + manual review) |
| Validation / settings | Pydantic v2, pydantic-settings |
| XML / XSD | `lxml` (FA(3) export and optional XSD validation) |
| Embeddings | `sentence-transformers` via the optional `embeddings` extra; deterministic hashing fallback for tests |
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
  api/               # routers: health, tax_params, calc, deadlines, decisions, documents,
                     # ksef export, period exports (JPK_PIT / tax pack)
  domain/            # pure enums, no infrastructure imports
  db/
    base.py          # DeclarativeBase + deterministic naming convention
    types.py         # JSONType, EmbeddingVector, sa_enum (cross-dialect)
    models.py        # all ORM models
    session.py       # lazy engine/session factories, FastAPI dependency
  services/          # tax params, pure calc engine, deadlines, ingest, classifier, storage
  integrations/      # inFakt / Fakturownia / wFirma adapters (Protocol only)
    ksef/            # FA(3) invoice builder + structural validation (no KSeF transmission)
    jpk/             # JPK_PIT builder + structural validation (no MF submission)
    schemas.py       # schema registry (config/schemas.yaml) + optional XSD validation
  assistant/         # allowlisted official-source ingestion, embeddings, answer protocols
config/
  tax_params/        # 2025.yaml, 2026.yaml - legal parameters as data
  schemas.yaml       # XSD urls/checksums + FA(3) format metadata
scripts/
  seed_tax_params.py # idempotent loader: YAML -> tax_year_params
  fetch_schemas.py   # download + checksum-verify the XSDs (resources/, gitignored)
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
| `DOCUMENTS_DIR` | `./documents` | Content-addressed upload storage root (`app/services/storage.py`); gitignored |
| `ASSISTANT_MIN_SCORE` | `0.25` | Cosine threshold for retrieval; below it the answer is `insufficient sources` |
| `ASSISTANT_TOP_K` | `5` | Chunks passed to the answer provider |
| `ASSISTANT_EMBEDDER` | `hashing` | `hashing` (deterministic) or `sentence-transformers` (optional extra) |
| `ASSISTANT_LLM` | `stub` | `stub` (deterministic, no network) or `openai` (OpenAI-compatible) |
| `ASSISTANT_LLM_API_KEY` | unset | Read from the environment only; never committed |
| `ASSISTANT_RATE_LIMIT_PER_MINUTE` | `20` | Per-client sliding window for `POST /api/assistant/ask` |

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
  (base × rate) must add up to `zus.total_full_pln` within a cent, each
  `zus.variants.<name>` must add up to its `monthly_total_pln`, `effective_from` must
  fall in the file's year, unknown keys are rejected, `source_url` must be a specific
  document (not a site root), and each `deadlines[].rule` must match its `kind`;
* `upsert(session, params)` → idempotent write keyed by `UNIQUE(year, version)`;
* the payload is stored as JSON with decimals serialised as exact strings.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/tax-params/{year}` | Stored, validated document of the highest version (404 when the year was never seeded) |

## Calculation preview

`app/services/calc.py` is a pure `Decimal` engine: every rate, threshold and contribution
base is read from the `TaxYearParams` document passed in, and no function touches the
database, clock or environment. Results are rounded to grosze with `ROUND_HALF_UP`;
non-positive tax bases yield zero, and invalid rates/months/variants raise
`CalcInputError`.

| Function | Result |
| --- | --- |
| `pit_scale(income, params)` | Progressive PIT using the scale brackets and the tax-free amount |
| `pit_linear(income, params)` | Flat-rate PIT |
| `pit_lump_sum(revenue, rate, params)` | Ryczalt at a rate configured in `pit.lump_sum` |
| `pit_ip_box(qualifying_income, params, eligible=...)` | IP BOX; raises `IncompatibleRegimeError` without eligibility |
| `vat_status_check(annual_turnover, params)` | `EXEMPT` at/below the threshold, `REGISTERED` above it |
| `vat_due(sales, costs, params)` | Output VAT minus input VAT (negative = credit) |
| `zus_contributions(month, variant, params)` | Monthly FULL / preferential / ulga na start contributions |

`POST /api/calc/preview` resolves the profile (PIT regime, ZUS variant, IP BOX eligibility)
and the stored parameters for the year, then returns the calculated amounts plus the
`source_url` of the document used:

```bash
curl -s -X POST localhost:8000/api/calc/preview -H 'Content-Type: application/json' -d '{
  "year": 2026,
  "profile_id": 1,
  "inputs": {
    "income": "120000.00",
    "annual_turnover": "240000.00",
    "sales": [{"net": "10000.00", "rate": "0.23"}],
    "costs": [{"net": "4000.00", "rate": "0.23"}],
    "month": 1
  }
}'
```

PIT for the profile's regime is required (`income` for scale/linear, `revenue` +
`lump_sum_rate` for ryczalt, `qualifying_income` for IP BOX). VAT status is returned when
`annual_turnover` is supplied, VAT due when `sales`/`costs` are supplied, and ZUS when
`month` is supplied. An empty list is a valid zero; omitting a field leaves that result
`null`. A profile that does not cover the year, or IP BOX without the eligibility flag,
returns `409`; unusable amounts/rates return `422`.

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

## Document intake

`POST /api/documents` accepts one multipart file (field `file`) of at most **10 MB** and
stores it content-addressed under `DOCUMENTS_DIR`:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/documents` | Upload a PDF/XML/CSV file; `201` with the row, `409` + `existing_id` on duplicate content |
| `PATCH /api/documents/{id}` | Correct the heuristic `type` and/or the `status` (`NEW`, `CLASSIFIED`, `VERIFIED`, `ARCHIVED`) |

The intake pipeline (`app/api/documents.py`, `app/services/classifier.py`,
`app/services/storage.py`) runs these steps before a row is created:

1. **Name** - only a bare file name with an allowlisted extension (`.pdf`, `.xml`,
   `.csv`) is accepted; separators, `..`, NUL bytes and hidden names are rejected with
   `400/415`, never sanitised into a path.
2. **MIME** - the declared content type must be plausible for the extension
   (`application/pdf`; `application/xml`, `text/xml`, `*+xml`; `text/csv`,
   `application/csv`, `text/plain`), otherwise `415`.
3. **Magic bytes** - the payload must actually look like the declared format (`%PDF-`,
   an XML prolog/root element, or decodable text for CSV), otherwise `415`; files above
   10 MB are rejected with `413` before they are buffered.
4. **Dedupe** - `sha256` of the bytes is looked up first; an existing row returns `409`
   with `{"detail": {"message": ..., "existing_id": <id>}}` and no second file is written.
5. **Write** - bytes go to a hidden temporary file in `DOCUMENTS_DIR`, are fsynced, then
   hard-linked as `<sha256><extension>`; `os.link` is atomic and refuses to overwrite, so
   no duplicate or partial file can appear.
6. **Classify** - ordered, case/diacritic-insensitive rules over the file name first and
   a 64 KB content prefix second (`faktura`, `FA(`, `JPK`, `ZUS DRA`, `PIT-`,
   `deklaracja`); anything unrecognised becomes `OTHER` and can be corrected via `PATCH`.

The client file name is used only as a classification hint and is never part of a
filesystem path. Logging is limited to the document id and byte size - contents, client
file names and storage paths are not logged.

A raw upload has no parsed amount/date yet, so `issue_date`/`period` default to the
Warsaw date and `amount_gross` is stored as `0.00`; extraction is a later pipeline step.

## Official knowledge base (versioned ingestion)

`app/assistant/ingest.py` builds the citation base from official pages only.
`ALLOWED_DOMAINS` is `podatki.gov.pl`, `zus.pl`, `isap.sejm.gov.pl` and `gov.pl`
(subdomains included); the host is checked on the requested URL, on **every redirect hop**
and again on the final URL, and only HTTPS is accepted. Lookalike hosts
(`podatki.gov.pl.evil.example`, `evil-podatki.gov.com`) and URLs with embedded credentials
are rejected before any request is made.

`ingest_url(session, url, embedder=..., fetcher=...)` then:

1. **Fetch** - `urllib` with a 30 s timeout, a 5 MB response cap and an allowlist-checking
   redirect handler; `fetcher` is injectable, so tests never touch the network.
2. **Extract** - the title (`<title>`, falling back to `<h1>`), the publication timestamp
   (`article:published_time`, `og:published_time`, `<meta name="date">`, `<time datetime>`)
   and the readable text of `<main>`/`<article>` (navigation, asides, scripts and the footer
   are dropped).
3. **Chunk** - windows of at most 800 characters with a 160-character overlap, ending on a
   paragraph, sentence or word boundary; `ordinal` is zero-based per source.
4. **Version** - `sha256(title + "\n\n" + text)` is the content identity. Identical content
   is a no-op; changed content inserts a new `knowledge_sources` row with `version + 1`
   while the previous row and its chunks stay untouched (citations remain reproducible).
   A digest already indexed under any URL is never stored twice, so `url` alone is not a
   unique key.
5. **Embed** - one `embedder.embed(chunk_texts)` call per new version; vectors are stored in
   `knowledge_chunks.embedding` as JSON text. The function flushes but never commits - the
   caller owns the transaction, like `tax_params.upsert`.

`app/assistant/embedder.py` defines the `Embedder` protocol (`embed(texts) ->
list[list[float]]`) with two implementations:

* `SentenceTransformerEmbedder` - the local, semantic model
  (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`), imported lazily so the
  default installation does not pull in torch. Install it with
  `pip install -e ".[embeddings]"`; a missing extra raises `EmbedderUnavailable`.
* `HashingEmbedder` - deterministic BLAKE2b feature hashing used by tests and offline runs;
  it detects that chunks are embedded but carries no semantic meaning.

```python
from app.assistant import embedder, ingest

result = ingest.ingest_url(
    session,
    "https://www.podatki.gov.pl/vat/stawki-vat-2026/",
    embedder=embedder.SentenceTransformerEmbedder(),
)
session.commit()
print(result.created, result.source.version, result.chunk_count)
```

The ingestion tests are offline: `pytest -q tests/test_assistant_ingest.py` uses the HTML
fixtures in `tests/fixtures/sources/` through a fake `Fetcher`.

## Assistant answers (citations, refusal, disclaimer)

`POST /api/assistant/ask` answers **only** from the versioned chunks above:

1. `app/assistant/retriever.py` embeds the question with the configured embedder, scores
   every chunk by cosine similarity and keeps the top `ASSISTANT_TOP_K` at or above
   `ASSISTANT_MIN_SCORE`. Sources outside the ingestion allowlist are skipped, so they can
   never become citations.
2. With no chunk above the threshold the answer is the literal `insufficient sources` and
   the model is **not called**.
3. Otherwise `app/assistant/answer.py` builds a prompt requiring a `[source_id]` marker and
   the source's `published_at` for every claim. The provider is selected by
   `ASSISTANT_LLM`: `stub` (default, deterministic) or `openai` (any OpenAI-compatible
   endpoint; key from `ASSISTANT_LLM_API_KEY`). The service re-derives citations from the
   markers, rejecting answers without a marker or with an unknown/non-allowlisted source.
4. Every answer, including refusals, is stored in `assistant_answers` with its citations
   and `disclaimer_shown=True`, and the API always returns the disclaimer.

```bash
curl -s -X POST localhost:8000/api/assistant/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Jakie są stawki VAT w 2026 roku?"}'
# {"answer": "... [1] (data publikacji: 2026-01-15)",
#  "citations": [{"source_id": 1, "url": "https://www.podatki.gov.pl/...",
#                 "title": "...", "published_at": "...", "fetched_at": "...",
#                 "version": "2", "chunk_ordinal": 0}],
#  "disclaimer": "Informacje mają charakter informacyjny ..."}
```

The endpoint rate-limits per client host (`429` + `Retry-After`) and its logs contain only
the question length, citation count and refusal flag - never the question or answer text.
`pytest -q tests/test_assistant_answer.py` covers coverage/refusal, the disclaimer on
every answer, allowlisted citation URLs and the source version.

## KSeF FA(3) export (no transmission)

`GET /api/documents/{id}/ksef-xml` renders a `VAT_INVOICE_SALES` document as an XML file in
the KSeF logical structure **FA(3)**. **The file is not sent to KSeF.** The tool never calls
the KSeF API, holds no KSeF credentials and does not sign anything: the response is meant
for review, signing and upload by an authorised tool. The guarantee is visible in the
response (`X-KSeF-Transmission: not-sent`) and inside the document itself
(`Naglowek/SystemInfo` = "plik wygenerowany lokalnie, NIE zostal przeslany do KSeF").

```bash
curl -s "localhost:8000/api/documents/1/ksef-xml?validate=true" \
  -H 'Content-Type: application/json' -o faktura.xml -D - -d '{
    "number": "FV/2026/02/1",
    "place": "Warszawa",
    "sale_date": "2026-02-10",
    "exemption_basis": "art. 43 ust. 1 pkt 3 ustawy o VAT",
    "seller": {"nip": "5260250274", "name": "TaxWise Jan Kowalski",
               "address_line_1": "ul. Kwiatowa 1", "address_line_2": "00-001 Warszawa"},
    "buyer":  {"nip": "9999999999", "name": "ABC AGD sp. z o.o.",
               "address_line_1": "ul. Polna 1", "address_line_2": "00-001 Warszawa"},
    "lines": [
      {"description": "Usluga doradcza", "quantity": "2", "unit": "szt.",
       "unit_net_price": "1000.00", "vat_rate": "23"},
      {"description": "Szkolenie", "quantity": "1", "unit": "usl.",
       "unit_net_price": "500.00", "vat_rate": "8"},
      {"description": "Swiadczenie zwolnione", "quantity": "1", "unit": "usl.",
       "unit_net_price": "250.00", "vat_rate": "zw"}
    ]
  }'
```

The stored row supplies the type, the issue date (`P_1`), the currency and the net/VAT/gross
amounts the XML must add up to. The body supplies what the skeleton does not store yet
(number, parties, lines, exemption basis). The request is read-only, hence `GET` with a body.

| Header | Meaning |
| --- | --- |
| `X-KSeF-Schema-Version` | FA(3) - taken from `config/schemas.yaml`, not from code |
| `X-Schema-Validation` | `ok` / `failed` / `skipped` (schema not downloaded - not an error) |
| `X-KSeF-Transmission` | `not-sent` - nothing left this process |

Validation layers:

1. **Builder checks** (`app/integrations/ksef/fa3_builder.py`) - required fields, NIP
   checksums, `JST`/`GV` flags, supported rates (`23`, `8`, `5`, `0`, `zw`) and the
   net/VAT/gross totals against the stored document. `Decimal` everywhere; net values are
   rounded to grosze per line, VAT per rate group (`P_14_x` taxes the group's net). Any
   difference is a `422` with the full violation list, never a silent correction.
2. **Structural validation** (`app/integrations/ksef/fa3_validate.py`) - required elements,
   types (dates, two-decimal amounts, integer row numbers, rate codes), `P_13_x`/`P_14_x`
   against the lines of that rate, and `P_15` against the sum of the groups.
3. **XSD validation** (optional, `?validate=true`) - `app/integrations/schemas.py` compiles
   the schema with `lxml` and returns `Violation(path="line N", message=...)`. No schema on
   disk -> `X-Schema-Validation: skipped` and a normal `200`; the schema also never travels
   over the network at request time.

The FA(3) metadata (namespace `http://crd.gov.pl/wzor/2025/06/25/13775/`,
`KodFormularza kodSystemowy="FA (3)" wersjaSchemy="1-0E"`, `WariantFormularza=3` and the
version label) lives in `config/schemas.yaml`; the builder reads it through
`schemas.fa3_format()`, so a format bump is a config change.

### Fetching the XSDs

```bash
python scripts/fetch_schemas.py             # download + verify every configured target
python scripts/fetch_schemas.py --check     # verify the local copies only
python scripts/fetch_schemas.py --only ksef_fa3
```

`config/schemas.yaml` records, per schema, the official `url`, the expected `sha256`, the
`version` and the target `path` (`resources/ksef/FA3.xsd`, `resources/jpk/JPK_PIT.xsd`).
Downloads are written only after the digest matches; a mismatch aborts with exit code `2`
and leaves the previous file untouched. `FA3.xsd` imports the shared eD definition files
(pinned with their own checksums), which are fetched next to it and resolved offline.
`resources/` is gitignored. `jpk_pit` is an optional entry without a verified official URL,
so the script reports it as **skipped** instead of failing. The existence of a separate
JPK_PIT XSD published by the MF requires confirmation at the source; until that is
confirmed the export marks XSD validation as `skipped` (see the JPK_PIT section below).

### Known limitations

* One VAT rate per line; rates outside `23/8/5/0/zw` (e.g. `oo`, `np`, WDT/export) are
  rejected rather than guessed.
* Only `RodzajFaktury=VAT` invoices; corrections (`KOR`), advances (`ZAL`) and margin
  procedures are not generated.
* Buyer identification is by NIP only (no `BrakID`/`UE` NIP variants), and the invoice
  carries no `Platnosc`/`WarunkiTransakcji` block.
* Totals are compared with the `documents` row, so a row without `amount_net`/`vat_amount`
  cannot be exported.


## JPK_PIT export and tax pack (no submission to MF)

`GET /api/exports/jpk-pit?year=&month=` maps the `VAT_INVOICE_SALES` documents whose
`period` is that month to `PRZYCHOD` rows and the `VAT_INVOICE_COST` documents to `KOSZT`
rows. Each row carries `typ`, `data` (the document `issue_date`), `kwota_netto`,
`kwota_vat`, `kwota_brutto` and `nip_kontrahenta`; no name or address is copied. Amounts are
`Decimal` end to end, written with two fraction digits, and rows are ordered by issue date
and id, so the same rows always render the same bytes.

```bash
curl -s "localhost:8000/api/exports/jpk-pit?year=2026&month=2" -o jpk_pit_2026_02.xml -D -
curl -s "localhost:8000/api/exports/tax-pack?year=2026" -o tax_pack_2026.zip -D -
```

| Endpoint | Response | Marking |
| --- | --- | --- |
| `GET /api/exports/jpk-pit?year=&month=` | `application/xml`, `jpk_pit_<year>_<month>.xml` | `X-JPK-PIT-Transmission: not-sent`, `X-Schema-Validation: ok/failed/skipped`, `X-JPK-PIT-Schema: unconfirmed` |
| `GET /api/exports/tax-pack?year=` | `application/zip`, `tax_pack_<year>.zip` | same marking; the archive holds `jpk_pit_<year>.xml`, `statement_<year>.csv` and `manifest.json` |

`manifest.json` lists every other file with its SHA-256 and byte size, plus
`jpk_pit_schema_status` and `submitted_to_mf: false`. ZIP entries use a fixed timestamp;
the manifest additionally records the generation time. File names contain only the period -
never a NIP, a company name or a document id.

Before a file is returned it passes two checks:

1. **Structural** (`app/integrations/jpk/jpk_pit_builder.validate_jpk_pit`) - the header
   (`KodFormularza`, `WariantFormularza`, `DataOd`, `DataDo`, `CelZlozenia`), ISO dates,
   two-decimal amounts, NIP checksums and `KwotaBrutto = KwotaNetto + KwotaVat` per row;
   `Podsumowanie` must equal both the parsed rows and the source documents. Any finding is
   a `422` with the full violation list.
2. **XSD** - `app/integrations/schemas.check` compiles the schema registered under
   `jpk_pit`. **No official JPK_PIT XSD is configured** (`url`/`sha256` are `null` in
   `config/schemas.yaml`): the existence of a separate JPK_PIT XSD requires confirmation at
   the source, so nothing is pinned and the status is `skipped`, never an error. Once a
   verified schema is added (and downloaded with `scripts/fetch_schemas.py`), a
   non-conforming file becomes a `422`.

A month without revenue/cost documents is a **valid statement with zero totals** (`Liczba*`
= 0 and every sum `0.00`): a missing period is not an error, it yields a file that can be
reviewed and filed.

Known limitations:

* The JPK_PIT shape is **provisional**: the namespace is `urn:taxwise:jpk-pit:1-0`, not a
  `crd.gov.pl` address, so the file cannot be mistaken for an official publication. When an
  official XSD is confirmed, replace the constants in `jpk_pit_builder` and fill the
  registry entry together.
* **The file is never submitted to MF.** There is no MF client, credential or signature in
  the codebase; the XML/ZIP is meant for review and manual upload by an authorised tool.
* Rows are selected by `documents.period` (`YYYY-MM`; the tax pack uses the `YYYY-` prefix,
  so a quarterly `2026-Q1` document is included in the year but not in a month).
* A revenue/cost row requires `amount_net`, `vat_amount`, `amount_gross = net + VAT`, a PLN
  currency and a checksum-valid counterparty NIP; a raw upload (`0.00` amounts, no NIP)
  must be completed first.
* Only revenue/cost documents are exported; ZUS/PIT/OTHER rows and attachments are ignored.
* Amounts are not converted: a foreign-currency document is rejected instead of being
  translated with an unknown rate.
* The CSV statement is UTF-8, `;`-separated and uses dot decimals (machine-readable, no
  locale-specific Excel formatting).


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
* `tests/test_calc.py` — scale thresholds and the tax-free amount, VAT threshold
  below/equal/above, every ZUS variant, IP BOX eligibility, grosz rounding, golden values
  from public MF/ZUS calculators (`tests/fixtures/calc_golden_values.yaml`, with source and
  read date), the no-rate-literals grep, and `POST /api/calc/preview` (all regimes,
  `source_url`, 404/409/422 paths).
* `tests/test_no_hardcoded_rates.py` — the policy grep above.
* `tests/test_ksef_fa3.py` — the golden FA(3) file (`tests/fixtures/fa3_expected.xml`),
  config-driven namespace/`KodFormularza`, per-rate totals, totals/field mismatches as
  `422` violations, structural validation (broken `P_15`, rate groups, namespace), and the
  export endpoint: `application/xml`, `X-KSeF-Schema-Version: FA(3)`,
  `X-Schema-Validation: ok|failed|skipped`, `X-KSeF-Transmission: not-sent`, 404/409/422.
* `tests/test_schema_validation.py` — the schema registry, XSD validation against
  `tests/fixtures/schemas/minimal.xsd`, SKIPPED without a schema, malformed XML as a
  violation, an unusable schema as an error, and `scripts/fetch_schemas.py` (checksum
  verification, atomic write, abort on mismatch, `--check`, optional targets).
* `tests/test_jpk_pit.py` — the golden JPK_PIT file (`tests/fixtures/jpk_pit_expected.xml`),
  revenue/cost mapping with `Decimal` amounts and counterparty NIPs, an empty month as a
  valid zero file, structural validation (header, amount types, row/summary consistency
  with the documents), `422` paths, `SKIPPED` when no JPK_PIT XSD is configured, and the
  tax-pack ZIP with a SHA-256 manifest.
* `tests/test_assistant_ingest.py` — allowlisted HTML fixture becomes a `KnowledgeSource`
  with embedded, overlapping chunks; changed content creates `version` 2 while the previous
  version and its chunks survive; unchanged content is not duplicated (and is not
  re-embedded); lookalike hosts, HTTP, embedded credentials and redirects outside the
  allowlist are rejected before any fetch; hashing embedder determinism.
* `tests/test_assistant_answer.py` — cosine top-k/threshold retrieval, a covered question
  returns citations with URL, dates and source version, no coverage returns
  `insufficient sources` without calling the model, every stored answer has
  `disclaimer_shown=True`, citations stay inside the allowlist, the prompt requires
  `[source_id]` + `published_at`, the LLM backend is env-swappable (stub default), the
  endpoint returns answer/citations/disclaimer, rate-limits per client and logs no
  question content.


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
* Upload dedupe is an application-level lookup on the `sha256` index, not a `UNIQUE`
  constraint, so two *concurrent* identical uploads can both insert; promoting the index
  to unique is a one-migration change when strict concurrency guarantees are needed.
  `file_path` stores the content-addressed name (`<sha256><extension>`), not an absolute
  path, so the storage root can be relocated or mounted elsewhere.
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
* ZUS figures in the YAML are planning values. `zus.components` is the "full" variant
  (including the minimum health contribution); `zus.variants` carries the preferential and
  "ulga na start" component sets. The actual health contribution depends on income and is
  computed elsewhere. Always re-check the values against `source_url`.
* The calculation engine is annual: `POST /api/calc/preview` requires the profile to be in
  force at the end of the requested year and takes the PIT base / turnover as inputs. It
  does not apply social/health contributions to the PIT base, does not pro-rate a partial
  year, and returns VAT as a signed balance (negative = credit) rather than carrying it
  forward. ZUS amounts are constant within a variant, so the `month` argument identifies the
  settlement period; eligibility windows are represented by the variant chosen for that
  month. IP BOX eligibility is the profile's `ip_box_rate` being set - the rate itself still
  comes from the yearly parameter document.
* `tax_profiles` validity ranges may overlap (no exclusion constraint, SQLite does not
  support one); services must select the row with the latest `valid_from` covering a date.
* Embeddings are JSON text and similarity search is expected to run in Python for now;
  replacing `EmbeddingVector` with a native vector type is a contained migration.
* Knowledge versions are identified by `sha256(title + text)`, so a page whose markup
  changes but whose readable text does not keeps its version; `fetched_at` records the
  re-fetch. The digest lookup is an application-level check (there is no `UNIQUE(sha256)`),
  so ingestion assumes a single writer. `HashingEmbedder` is a deterministic test/offline
  fallback, not a semantic model - production should install the `embeddings` extra.
* Only HTML pages are ingested; a response without `<main>`/`<article>`/body text or with an
  explicit non-HTML content type is rejected instead of indexed empty. PDFs and XSDs stay
  with the document intake and `scripts/fetch_schemas.py` respectively.
* `/health` is a liveness probe without a database check by design; a separate readiness
  probe should query the DB when deployment needs it.
* No authentication/authorisation layer is included in the skeleton.
* The FA(3) export **never transmits anything to KSeF**: there is no KSeF client, session or
  token in the codebase. Generated XML is unsigned and must be reviewed and uploaded by an
  authorised tool.
* `GET /api/documents/{id}/ksef-xml` takes a JSON body because the `documents` row stores
  only aggregates; once invoices are parsed into line items the body can become optional.
  `?validate=true` reads a local XSD and never fetches one.
* The JPK_PIT export **never submits anything to MF**: there is no MF client, credential or
  signature in the codebase. The JPK_PIT format is provisional (local `urn:` namespace) and
  its XSD is not pinned because the existence of a separate MF JPK_PIT schema requires
  confirmation at the source; the export reports XSD validation as `skipped` until a
  verified schema is configured.

## Verification

```bash
alembic upgrade head && python scripts/seed_tax_params.py \
  && python scripts/fetch_schemas.py \
  && pytest -q && ruff check . && mypy app \
  && curl -s localhost:8000/api/tax-params/2026 && curl -s localhost:8000/health
```

Expected: migration applies, the seed script reports created/updated rows, the schema
script fetches and verifies the FA(3) XSDs (`resources/`, optional), all tests pass,
linters clean, `/api/tax-params/2026` returns the stored document with `source_url` and
`effective_from`, and `/health` returns
`{"status":"ok","app":"TaxWise SME API","version":"0.1.0","environment":"local"}`.

The KSeF export is covered by `pytest -q tests/test_ksef_fa3.py tests/test_schema_validation.py`;
the second file needs `scripts/fetch_schemas.py` for the end-to-end check against the
official schema and skips that one test otherwise. The JPK_PIT export and tax pack are
covered by `pytest -q tests/test_jpk_pit.py` (no XSD download is needed - validation is
expected to report `skipped`). Knowledge-base ingestion is covered by
`pytest -q tests/test_assistant_ingest.py`; it needs no network access because the HTML
fixtures live in `tests/fixtures/sources/`.

