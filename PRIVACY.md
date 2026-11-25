# Privacy notice and data processing notes

TaxWise SME is a self-hosted finance assistant for Polish sole proprietorships. This
document describes **what the application stores, why, for how long and which controls it
provides** for data-protection requests. It is an engineering description, not legal
advice; the operator deploying the application decides the legal basis and must adapt this
text to its own records of processing.

The application has no user accounts, tenants or authentication layer. The operator that
deploys it is the **data controller**; the application is a local tool, and no data is sent
to the project authors or to a third party by default.

## 1. Data scope

| Category | What is stored | Where | Why |
| --- | --- | --- | --- |
| Documents | `documents` row: type, status, source, period, issue date, net/VAT/gross amounts, currency, contractor NIP, external id, content-addressed file name, SHA-256, timestamps | database + `DOCUMENTS_DIR` file | Bookkeeping, VAT/PIT calculations, exports |
| Document files | The uploaded PDF/XML/CSV bytes, named `<sha256><extension>` (the client file name is only used for classification and is never stored) | `DOCUMENTS_DIR` | Source evidence for the stored aggregates |
| Tax profile | PIT regime, VAT status, ZUS variant, VAT threshold, IP BOX rate, validity dates | `tax_profiles` | Determines which calculation rules apply |
| Deadlines | Kind, period, due date, status, free-text notes | `deadlines` | Filing/payment calendar |
| Decisions | Topic, choice, rationale, source URLs, timestamp | `decisions` | Auditable record of tax decisions |
| Assistant Q&A | Question, answer, citations, model name, timestamp, disclaimer flag | `assistant_answers` | Answer history and citation audit |
| Audit trail | Actor label, action, entity, entity id, timestamp, small JSON `meta` | `audit_log` | Security/accountability; designed to contain **no personal data** |
| Tax parameters | Public legal rates/thresholds with an official `source_url` | `tax_year_params` | Reproducible calculations (public data, not personal) |
| Knowledge base | Public MF/ZUS/ISAP publications, chunks and embeddings | `knowledge_sources`, `knowledge_chunks` | Citation-backed answers (public data) |

The application does **not** store passwords, e-mail addresses, IP addresses, user-agents
or free-form client file names. The uploaded file content may itself contain personal data
(names, addresses, PESEL, e-mail); that is the user's data and is processed as part of the
document.

## 2. What is deliberately not in the audit trail or logs

`app/services/audit.py` accepts only allowlisted `meta` keys and scalar values, and rejects
strings shaped like an e-mail address, a 10-digit NIP or an 11-digit PESEL. Audit rows
contain counts, enum values, years and cutoff dates - no document contents, file names,
contractor names or identifiers. Application log lines carry the action, entity label and
database id only; document contents, client file names and storage paths are never logged.

A reverse proxy in front of the application may still produce standard access logs with IP
addresses. That configuration is outside this repository and must be covered by the
operator's retention and privacy settings.

## 3. Purposes and legal bases

Typical bases for a sole proprietor using the tool:

* **Contract / steps prior to a contract** (Art. 6(1)(b) GDPR) and **legal obligation**
  (Art. 6(1)(c)) for accounting, VAT and tax records;
* **Legitimate interest** (Art. 6(1)(f)) for security, auditability and defending claims;
* **Consent** where the operator chooses to rely on it for optional assistant questions.

The operator must document the actual basis, especially for any special-category data that
a user might upload. The application does not detect or filter such data.

## 4. Retention

* The default retention window is **5 years** (`RETENTION_YEARS`, configurable 1-50) and is
  measured from `documents.created_at` in UTC - storage time, not the end of the tax year.
  Set the window conservatively (the statutory period runs from the end of the tax year).
* `POST /api/admin/retention/run?dry_run=true` is **read-only**: it lists the expired
  documents and their file names, and changes nothing.
* `POST /api/admin/retention/run?dry_run=false` irreversibly deletes the expired files and
  rows. It requires the `X-Retention-Token` header matching
  `RETENTION_CONFIRMATION_TOKEN`; a missing, wrong or unconfigured token returns `403` and
  changes nothing. Every completed purge appends one `RETENTION_PURGE` audit row.
* Deletion is **not** scheduled automatically. An operator must review a dry run and start
  the purge (or call the service from a scheduler) and must ensure that the configured
  window is not shorter than the statutory accounting retention period.
* A file still referenced by a retained document is kept. If a file is already missing,
  the row is still deleted; the response reports the number of files actually removed.
* **Backups are not touched by the purge.** Rotate/expire database and `DOCUMENTS_DIR`
  backups under the same policy, otherwise deleted personal data can reappear after a
  restore.

The audit trail itself is retained with the database. It contains no personal data by
design, so it is not part of the erasure scope; operators may still apply their own
housekeeping to it.

## 5. Data-subject rights

| Right | How the application supports it |
| --- | --- |
| Access / copy (Art. 15) | `GET /api/admin/export-data` returns every stored category, including documents, profiles, deadlines, decisions, answers and the audit trail, as one JSON file with counts |
| Rectification (Art. 16) | `PATCH /api/documents/{id}` corrects a document's type/status; profile, deadline and decision rows can be corrected through the corresponding endpoints/services |
| Erasure (Art. 17) | A confirmed retention purge deletes expired files and rows; an operator can lower `RETENTION_YEARS` for a reviewed run. Statutory accounting retention can override erasure, so erasure must be assessed per document |
| Restriction (Art. 18) | There is no dedicated "freeze" flag; the operator must take the instance out of service or move the row to an isolated database |
| Portability (Art. 20) | The JSON export is machine-readable; document files are available under their content-addressed names in `DOCUMENTS_DIR` |
| Objection (Art. 21) | Relevant where processing relies on legitimate interest; handled by the operator |
| Withdrawal of consent | Handled by the operator; it does not retroactively affect statutory accounting records |
| Complaint | The data subject may complain to the Polish supervisory authority (UODO, ul. Stawki 2, 00-193 Warszawa) |

Requests should be answered within one month (extendable by two months for complex
requests). The export **contains personal data**: transmit it over an encrypted channel,
do not log it, and delete the generated file when the request is closed.

## 6. Security measures in this repository

* Content-addressed storage with an extension allowlist, filename sanitisation, path
  containment checks and atomic writes (`app/services/storage.py`).
* Upload validation (bare file name, MIME type, magic bytes, 10 MB limit) and deduplication
  by SHA-256.
* Irreversible deletion gated by an environment token compared in constant time; the token
  is accepted only in a header, never in a URL.
* PII-free, append-only audit rows with a closed metadata vocabulary.
* No transmission to MF or KSeF: exports are rendered locally for manual review/upload.
* Structured settings (`app/settings.py`); secrets are `SecretStr` and read from the
  environment, never committed.

## 7. Limitations and operator responsibilities

* **No authentication/authorisation**: anyone who can reach the API can read the export
  and documents. Deploy behind an authenticated reverse proxy, bind to localhost or use a
  private network; the retention token protects only the purge endpoint.
* **No multi-tenancy**: the database is a single taxpayer's data. Do not point one instance
  at several unrelated businesses.
* **No automatic retention scheduler and no legal-hold handling**: a purge can remove
  records that must still be kept if `RETENTION_YEARS` is misconfigured.
* **Irreversible by design**: there is no trash or undo for a confirmed purge; only a backup
  can restore the data.
* **Backups and reverse-proxy logs are out of scope** of the in-application retention run.
* **No encryption at rest** is provided by the application; use encrypted volumes and TLS.
* **No DPIA, records of processing, DPA or data-subject-request workflow** is shipped; the
  operator must prepare them.
* **Assistant answers can be wrong**: outputs are informational, include a disclaimer and
  are not filed with any authority automatically.
* **The export is a snapshot**: rows written after the export (including its own
  `EXPORT_DATA` audit entry) are not part of the returned JSON.

## 8. Contact

Replace this placeholder with the operator's contact details before deployment:

```
Data controller: <company / name, address, NIP>
Privacy contact: <e-mail / phone>
```
