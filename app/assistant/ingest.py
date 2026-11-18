"""Versioned ingestion of official tax knowledge into the assistant's knowledge base.

Only pages published on the domains in :data:`ALLOWED_DOMAINS` may be fetched. The check
is applied to the URL handed in by the caller, to *every* redirect hop (see
:class:`_AllowlistRedirectHandler`) and again to the final URL returned by the fetcher, so
a redirect cannot smuggle content from another host into the base.

One page becomes one :class:`~app.db.models.KnowledgeSource` row plus its
:class:`~app.db.models.KnowledgeChunk` rows:

* the readable text (``<main>``/``<article>`` body, chrome removed) is split into windows
  of at most 800 characters with a 160-character overlap;
* the SHA-256 of ``title + text`` identifies the content version. Re-ingesting identical
  content is a no-op; changed content inserts a *new* row with ``version + 1`` and leaves
  the previous version and its chunks untouched, so citations stay reproducible;
* the same digest is never stored twice - identical content already indexed under another
  URL is reported as the existing source instead of creating a duplicate.

Embeddings are computed once per new version and written through the ``embedding`` column
(JSON text, see :mod:`app.db.types`). :func:`ingest_url` flushes but never commits: the
caller owns the transaction, like :func:`app.services.tax_params.upsert`.

Concurrency note: the digest lookup and the version scan assume a single ingestion writer
process; the schema has no unique constraint on ``sha256``.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.client import HTTPMessage
from typing import IO, Final
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.embedder import Embedder
from app.db.models import KnowledgeChunk, KnowledgeSource
from app.domain.enums import KnowledgePublisher

__all__ = [
    "ALLOWED_DOMAINS",
    "ALLOWED_SCHEMES",
    "CHUNK_MAX_CHARS",
    "CHUNK_OVERLAP_CHARS",
    "FETCH_TIMEOUT_SECONDS",
    "MAX_PAGE_BYTES",
    "ContentError",
    "DomainNotAllowed",
    "ExtractedPage",
    "FetchError",
    "FetchedPage",
    "Fetcher",
    "IngestError",
    "IngestResult",
    "chunk_text",
    "extract_page",
    "fetch_page",
    "ingest_url",
    "is_allowed_host",
    "publisher_for_url",
    "validate_url",
]

logger = logging.getLogger(__name__)

#: The only hosts pages may be fetched from; subdomains (``www.``, ``isap.``) are allowed.
ALLOWED_DOMAINS: Final[frozenset[str]] = frozenset(
    {"podatki.gov.pl", "zus.pl", "isap.sejm.gov.pl", "gov.pl"}
)

#: Official sources are fetched over TLS only; plain HTTP links must be upgraded first.
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"https"})

#: Target chunk size and the context repeated from the previous chunk.
CHUNK_MAX_CHARS: Final[int] = 800
CHUNK_OVERLAP_CHARS: Final[int] = 160

#: Responses larger than this are rejected instead of being buffered indefinitely.
MAX_PAGE_BYTES: Final[int] = 5 * 1024 * 1024

#: Read timeout for a single page request.
FETCH_TIMEOUT_SECONDS: Final[float] = 30.0

USER_AGENT: Final[str] = "taxwise-sme/knowledge-ingest (official sources)"

#: Elements that never carry indexable body text.
_SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "aside",
        "button",
        "footer",
        "form",
        "iframe",
        "nav",
        "noscript",
        "script",
        "select",
        "style",
        "svg",
        "template",
    }
)

#: Elements that delimit readable blocks; a blank line is emitted around each one.
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

#: Preferred content roots; fall back to the whole body when neither is present.
_CONTENT_TAGS: Final[frozenset[str]] = frozenset({"main", "article"})

#: ``<meta>`` keys that may carry the publication timestamp.
_PUBLISHED_META_KEYS: Final[frozenset[str]] = frozenset(
    {
        "article:published_time",
        "date",
        "datepublished",
        "dc.date",
        "dcterms.created",
        "dcterms.date",
        "og:published_time",
        "pubdate",
        "publish-date",
        "publication_date",
    }
)

_META_CHARSET: Final[re.Pattern[bytes]] = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", re.IGNORECASE
)

#: Naive publication dates seen in official pages; ISO-8601 is handled separately.
_NAIVE_DATE_FORMATS: Final[tuple[str, ...]] = ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d")


class IngestError(Exception):
    """Base class for ingestion failures that callers may report to an operator."""


class DomainNotAllowed(IngestError):
    """The URL (or a redirect target) points outside :data:`ALLOWED_DOMAINS`."""


class FetchError(IngestError):
    """The page could not be downloaded."""


class ContentError(IngestError):
    """The downloaded response is not a non-empty HTML page with readable text."""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A fetched response; ``final_url`` is the URL after redirects."""

    final_url: str
    content_type: str | None
    body: bytes
    charset: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """Readable parts of an HTML page."""

    title: str
    text: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of :func:`ingest_url`; ``created`` is false for unchanged content."""

    source: KnowledgeSource
    created: bool
    chunk_count: int


#: A page fetcher: given a URL, return its final URL, headers and bytes.
type Fetcher = Callable[[str], FetchedPage]


def is_allowed_host(host: str, *, allowed_domains: frozenset[str] = ALLOWED_DOMAINS) -> bool:
    """Return whether ``host`` is an allowlisted domain or one of its subdomains.

    Matching is done on label boundaries, so ``evil-zus.pl`` and ``zus.pl.evil.example``
    are rejected while ``www.zus.pl`` is accepted.
    """
    candidate = host.strip().rstrip(".").lower()
    if not candidate:
        return False
    return any(
        candidate == domain or candidate.endswith(f".{domain}") for domain in allowed_domains
    )


def validate_url(url: str) -> str:
    """Return the canonical HTTPS form of ``url`` or raise :class:`DomainNotAllowed`.

    Fragments are dropped (they are never sent to the server), the host is lowercased and
    an empty path becomes ``/``. Credentials and non-HTTPS schemes are refused so the
    stored URL always identifies the exact official document that was fetched.
    """
    candidate = url.strip()
    if not candidate:
        raise DomainNotAllowed("empty URL")
    try:
        parts = urlsplit(candidate)
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise DomainNotAllowed(f"malformed URL: {url!r}") from exc
    if parts.scheme not in ALLOWED_SCHEMES:
        raise DomainNotAllowed(
            f"{url!r}: only {sorted(ALLOWED_SCHEMES)} URLs are fetched, got {parts.scheme!r}"
        )
    if parts.username is not None or parts.password is not None:
        raise DomainNotAllowed(f"{url!r}: URLs with embedded credentials are not allowed")
    if not is_allowed_host(host):
        raise DomainNotAllowed(f"host {host!r} is outside the allowlist {sorted(ALLOWED_DOMAINS)}")
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target before urllib follows it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url: str, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> FetchedPage:
    """Download one allowed HTML page over HTTPS; redirects are allowlist-checked.

    The response body is capped at :data:`MAX_PAGE_BYTES`; the final URL is re-validated so
    even a misbehaving redirect chain can never return content from another host.
    """
    target = validate_url(url)
    opener = urllib.request.build_opener(_AllowlistRedirectHandler())
    request = urllib.request.Request(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = validate_url(response.geturl())
            headers = response.headers
            content_type = headers.get("Content-Type")
            charset = headers.get_content_charset()
            body = response.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{target}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise FetchError(f"{target}: download failed: {exc}") from exc
    if len(body) > MAX_PAGE_BYTES:
        raise FetchError(f"{target}: response exceeds {MAX_PAGE_BYTES} bytes")
    return FetchedPage(final_url=final_url, content_type=content_type, body=body, charset=charset)


def publisher_for_url(url: str) -> KnowledgePublisher:
    """Map an allowlisted host to the publisher stored on ``KnowledgeSource``.

    ``gov.pl`` outside ``podatki.gov.pl`` is generic government content, so it becomes
    ``OTHER`` instead of guessing a ministry.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host == "zus.pl" or host.endswith(".zus.pl"):
        return KnowledgePublisher.ZUS
    if host == "isap.sejm.gov.pl":
        return KnowledgePublisher.ISAP
    if host == "podatki.gov.pl" or host.endswith(".podatki.gov.pl"):
        return KnowledgePublisher.MF
    return KnowledgePublisher.OTHER


def _looks_like_html(content_type: str | None) -> bool:
    """Accept HTML/XHTML and unknown content types, reject explicit other formats."""
    if not content_type:
        return True
    main_type = content_type.split(";", 1)[0].strip().lower()
    return not main_type or main_type in {"text/html", "application/xhtml+xml"}


def _meta_charset(body: bytes) -> str | None:
    match = _META_CHARSET.search(body[:2048])
    if match is None:
        return None
    return match.group(1).decode("ascii", errors="ignore") or None


def _decode_body(body: bytes, charset: str | None) -> str:
    """Decode with the declared charset, then UTF-8/CP1250, then a lossy UTF-8 pass."""
    candidates = (charset, _meta_charset(body), "utf-8", "cp1250")
    seen: set[str] = set()
    for encoding in candidates:
        if not encoding:
            continue
        normalized = encoding.strip().lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return body.decode(normalized)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _collapse(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _normalize_text(raw: str) -> str:
    """Collapse inline whitespace but keep paragraph breaks as blank lines."""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in normalized.split("\n"):
        stripped = " ".join(line.split())
        if stripped:
            buffer.append(stripped)
        elif buffer:
            paragraphs.append(" ".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))
    return "\n\n".join(paragraphs)


def _parse_datetime(value: str) -> datetime | None:
    """Parse ISO-8601 or the common Polish date formats into an aware datetime."""
    candidate = value.strip()
    if not candidate:
        return None
    cleaned = candidate[:-1] + "+00:00" if candidate.endswith(("Z", "z")) else candidate
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        parsed = None
    if parsed is None:
        for date_format in _NAIVE_DATE_FORMATS:
            try:
                parsed = datetime.strptime(candidate, date_format)
            except ValueError:
                continue
            break
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class _HtmlExtractor(HTMLParser):
    """Collect the title, publication date and readable text from one HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_parts: list[str] = []
        self._heading_parts: list[str] = []
        self._body_parts: list[str] = []
        self._content_parts: list[str] = []
        self._published_candidates: list[str] = []
        self._in_title = False
        self._heading_depth = 0
        self._content_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth += 1
            return
        attributes = {name: value or "" for name, value in attrs}
        if tag == "meta":
            self._record_meta(attributes)
            return
        if tag == "time" and attributes.get("datetime"):
            self._published_candidates.append(attributes["datetime"])
        if tag in _SKIP_TAGS:
            self._skip_depth = 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "h1":
            self._heading_depth += 1
        if tag in _CONTENT_TAGS:
            self._content_depth += 1
        if tag in _BLOCK_TAGS:
            self._append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._append("\n\n")
        if tag == "h1":
            self._heading_depth = max(0, self._heading_depth - 1)
        if tag in _CONTENT_TAGS:
            self._content_depth = max(0, self._content_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._body_parts.append(data)
        if self._content_depth:
            self._content_parts.append(data)
        if self._heading_depth:
            self._heading_parts.append(data)

    def _append(self, separator: str) -> None:
        self._body_parts.append(separator)
        if self._content_depth:
            self._content_parts.append(separator)

    def _record_meta(self, attributes: dict[str, str]) -> None:
        key = (
            (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            )
            .strip()
            .lower()
        )
        content = attributes.get("content", "").strip()
        if key and content and (key in _PUBLISHED_META_KEYS or key.endswith("published_time")):
            self._published_candidates.append(content)

    def title(self) -> str:
        return _collapse(self._title_parts) or _collapse(self._heading_parts)

    def text(self) -> str:
        content = _normalize_text("".join(self._content_parts))
        return content or _normalize_text("".join(self._body_parts))

    def published_at(self) -> datetime | None:
        for candidate in self._published_candidates:
            parsed = _parse_datetime(candidate)
            if parsed is not None:
                return parsed
        return None


def extract_page(page: FetchedPage) -> ExtractedPage:
    """Turn an HTML response into title, readable text and publication timestamp."""
    if not _looks_like_html(page.content_type):
        raise ContentError(
            f"{page.final_url}: unsupported content type {page.content_type!r}; expected HTML"
        )
    if not page.body:
        raise ContentError(f"{page.final_url}: the response body is empty")
    extractor = _HtmlExtractor()
    extractor.feed(_decode_body(page.body, page.charset))
    extractor.close()
    text = extractor.text()
    if not text:
        raise ContentError(f"{page.final_url}: the page contains no indexable text")
    return ExtractedPage(title=extractor.title(), text=text, published_at=extractor.published_at())


def _find_boundary(text: str, floor: int, ceiling: int) -> int | None:
    """Return where to end a chunk between ``floor`` and ``ceiling`` (exclusive end)."""
    paragraph = text.rfind("\n\n", floor, ceiling)
    if paragraph != -1:
        return paragraph
    sentence = max(
        text.rfind(". ", floor, ceiling),
        text.rfind("! ", floor, ceiling),
        text.rfind("? ", floor, ceiling),
    )
    if sentence != -1:
        return sentence + 1
    space = text.rfind(" ", floor, ceiling)
    return space if space != -1 else None


def chunk_text(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split ``text`` into overlapping windows of at most ``max_chars`` characters.

    Windows end on a paragraph, sentence or word boundary when one is available in the
    second half of the window, so a chunk does not start mid-word. Consecutive chunks share
    roughly ``overlap`` characters, which keeps a sentence split across a boundary
    retrievable from either side.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be in [0, max_chars)")
    normalized = _normalize_text(text)
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            boundary = _find_boundary(normalized, start + max_chars // 2, end)
            if boundary is not None:
                end = boundary
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        next_start = end - overlap
        space = normalized.find(" ", next_start, end)
        if space != -1:
            next_start = space + 1
        start = max(next_start, start + 1)
    return chunks


def _content_sha256(title: str, text: str) -> str:
    """Digest of the canonical content, ignoring markup, navigation and fetch metadata."""
    return hashlib.sha256(f"{title}\n\n{text}".encode()).hexdigest()


def _next_version(session: Session, url: str) -> str:
    """Return the next numeric version for ``url``; ``"1"`` for a new URL."""
    versions = session.scalars(
        select(KnowledgeSource.version).where(KnowledgeSource.url == url)
    ).all()
    numbers = [int(value) for value in versions if value is not None and value.isdigit()]
    return str(max(numbers, default=0) + 1)


def ingest_url(
    session: Session,
    url: str,
    *,
    embedder: Embedder,
    fetcher: Fetcher = fetch_page,
    fetched_at: datetime | None = None,
) -> IngestResult:
    """Fetch, extract, chunk and version one official page.

    The URL is allowlist-checked before the fetcher is called and the fetcher's final URL
    is checked again, so a redirect outside :data:`ALLOWED_DOMAINS` never reaches the
    parser. Identical content returns the existing source (``created=False``); changed
    content inserts a new source version with fresh chunks. The session is flushed, not
    committed - the caller decides when the transaction ends.
    """
    requested_url = validate_url(url)
    page = fetcher(requested_url)
    final_url = validate_url(page.final_url)
    extracted = extract_page(page)
    title = extracted.title or final_url
    digest = _content_sha256(title, extracted.text)

    existing = session.scalars(
        select(KnowledgeSource).where(KnowledgeSource.sha256 == digest)
    ).first()
    if existing is not None:
        logger.info("knowledge source already indexed: id=%s sha256=%s", existing.id, digest)
        return IngestResult(source=existing, created=False, chunk_count=len(existing.chunks))

    pieces = chunk_text(extracted.text)
    if not pieces:
        raise ContentError(f"{final_url}: the page contains no indexable text")
    vectors = embedder.embed(pieces)
    if len(vectors) != len(pieces):
        raise IngestError(f"embedder returned {len(vectors)} vectors for {len(pieces)} chunks")

    source = KnowledgeSource(
        publisher=publisher_for_url(final_url),
        url=final_url,
        title=title,
        published_at=extracted.published_at,
        fetched_at=fetched_at or datetime.now(UTC),
        sha256=digest,
        version=_next_version(session, final_url),
    )
    session.add(source)
    session.flush()  # assigns source.id needed by the chunk foreign keys
    source.chunks = [
        KnowledgeChunk(ordinal=ordinal, text=piece, embedding=list(vector))
        for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
    ]
    session.flush()
    logger.info(
        "knowledge source ingested: id=%s version=%s chunks=%d",
        source.id,
        source.version,
        len(pieces),
    )
    return IngestResult(source=source, created=True, chunk_count=len(pieces))
