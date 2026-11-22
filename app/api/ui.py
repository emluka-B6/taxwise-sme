"""Minimal server-rendered UI for reviewing the data collected by the API.

The pages are deliberately thin: every action goes through an existing API handler
(:func:`app.api.documents.upload_document`, :func:`app.api.deadlines.update_deadline`,
:func:`app.api.assistant.ask`), so validation, status derivation and persistence stay in
one place. Templates only iterate over the returned models - no tax or lifecycle rules and
no inline JavaScript carrying user data.

Jinja2 is configured with ``autoescape=True`` and ``StrictUndefined`` so every value is
escaped by default and a missing context variable fails loudly instead of rendering an
empty cell. The only logging happens in the API layer, which never records file names,
questions or answers.

Routes:

* ``GET /documents`` - filterable document register;
* ``POST /documents`` - upload through the document endpoint (HTMX or plain form);
* ``GET /deadlines`` - deadline register with the derived status;
* ``PATCH|POST /deadlines/{id}`` - status change through the deadline endpoint;
* ``GET|POST /assistant`` - question form answered by the assistant endpoint, with
  citations and the disclaimer rendered in every response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

import jinja2
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import assistant as assistant_api
from app.api import deadlines as deadlines_api
from app.api import documents as documents_api
from app.assistant.answer import DISCLAIMER
from app.assistant.base import Answerer
from app.assistant.embedder import Embedder
from app.db.models import Document
from app.db.session import get_session
from app.domain.enums import (
    CURRENT_DEADLINE_STATUSES,
    DeadlineStatus,
    DocumentStatus,
    DocumentType,
)
from app.settings import Settings, get_settings

__all__ = ["router", "templates"]

router = APIRouter(tags=["ui"], include_in_schema=False)

#: Templates live next to the package, so the path does not depend on the CWD.
TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "templates"

#: Autoescaping is on for *every* template; templates are trusted, user data never is.
templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        auto_reload=False,
    )
)

#: Filter options rendered as ``<option>`` lists; the enum remains the source of truth.
DOCUMENT_TYPES: Final[tuple[str, ...]] = tuple(member.value for member in DocumentType)
DOCUMENT_STATUSES: Final[tuple[str, ...]] = tuple(member.value for member in DocumentStatus)
WRITABLE_DEADLINE_STATUSES: Final[tuple[str, ...]] = tuple(
    sorted(member.value for member in CURRENT_DEADLINE_STATUSES)
)


def _is_hx(request: Request) -> bool:
    """Whether HTMX issued the request (the fragment is then rendered without the layout)."""
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _render(
    request: Request,
    template_name: str,
    *,
    page_title: str,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    """Render a full page or an HTMX fragment (templates switch on ``hx_request``)."""
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "request": request,
            "hx_request": _is_hx(request),
            "page_title": page_title,
            **context,
        },
        status_code=status_code,
    )


def _error_text(detail: object) -> str:
    """Flatten an API error (string or ``{"message": ...}``) into display text."""
    if isinstance(detail, dict):
        message = detail.get("message")
        if message is not None:
            return str(message)
    return str(detail)


def _document_type(value: str | None) -> DocumentType | None:
    """Resolve the ``type`` filter; an empty or unknown value means 'all'."""
    try:
        return DocumentType((value or "").strip())
    except ValueError:
        return None


def _document_status(value: str | None) -> DocumentStatus | None:
    """Resolve the ``status`` filter; an empty or unknown value means 'all'."""
    try:
        return DocumentStatus((value or "").strip())
    except ValueError:
        return None


def _deadline_status(value: str | None) -> DeadlineStatus | None:
    """Resolve the deadline filter; an empty or unknown value means 'all'."""
    try:
        return DeadlineStatus((value or "").strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- documents


def _list_documents(
    session: Session,
    *,
    document_type: DocumentType | None,
    document_status: DocumentStatus | None,
) -> list[Document]:
    """Register rows, newest first; filters are applied in SQL, not in the template."""
    statement = select(Document).order_by(Document.created_at.desc(), Document.id.desc())
    if document_type is not None:
        statement = statement.where(Document.type == document_type)
    if document_status is not None:
        statement = statement.where(Document.status == document_status)
    return list(session.scalars(statement))


def _documents_response(
    request: Request,
    session: Session,
    *,
    document_type: DocumentType | None,
    document_status: DocumentStatus | None,
    error: str | None = None,
) -> HTMLResponse:
    return _render(
        request,
        "documents.html",
        page_title="Dokumenty",
        documents=_list_documents(
            session, document_type=document_type, document_status=document_status
        ),
        document_types=DOCUMENT_TYPES,
        document_statuses=DOCUMENT_STATUSES,
        type_filter=document_type,
        status_filter=document_status,
        error=error,
    )


@router.get("/documents", response_class=HTMLResponse, name="documents_page")
def documents_page(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    document_type: Annotated[str | None, Query(alias="type")] = None,
    document_status: Annotated[str | None, Query(alias="status")] = None,
) -> HTMLResponse:
    """Document register with optional type/status filters."""
    return _documents_response(
        request,
        session,
        document_type=_document_type(document_type),
        document_status=_document_status(document_status),
    )


@router.post("/documents", response_class=HTMLResponse, name="documents_upload")
def upload_document_page(
    request: Request,
    file: Annotated[UploadFile, File(description="PDF, XML or CSV file, max 10 MB.")],
    session: Annotated[Session, Depends(get_session)],
    document_type: Annotated[str | None, Form(alias="type")] = None,
    document_status: Annotated[str | None, Form(alias="status")] = None,
) -> HTMLResponse:
    """Upload through the documented endpoint, then re-render the current register."""
    error: str | None = None
    try:
        documents_api.upload_document(file=file, session=session)
    except HTTPException as exc:
        error = _error_text(exc.detail)
    return _documents_response(
        request,
        session,
        document_type=_document_type(document_type),
        document_status=_document_status(document_status),
        error=error,
    )


# --------------------------------------------------------------------------- deadlines


def _deadlines_response(
    request: Request,
    session: Session,
    *,
    deadline_status: DeadlineStatus | None,
    error: str | None = None,
) -> HTMLResponse:
    return _render(
        request,
        "deadlines.html",
        page_title="Terminy",
        deadlines=deadlines_api.list_deadlines(session, status=deadline_status),
        deadline_statuses=WRITABLE_DEADLINE_STATUSES,
        status_filter=deadline_status,
        error=error,
    )


@router.get("/deadlines", response_class=HTMLResponse, name="deadlines_page")
def deadlines_page(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    deadline_status: Annotated[str | None, Query(alias="status")] = None,
) -> HTMLResponse:
    """Deadline register with an optional effective-status filter."""
    return _deadlines_response(
        request, session, deadline_status=_deadline_status(deadline_status)
    )


@router.api_route(
    "/deadlines/{deadline_id}",
    methods=["PATCH", "POST"],
    response_class=HTMLResponse,
    name="deadline_status",
)
def update_deadline_page(
    request: Request,
    deadline_id: deadlines_api.DeadlineIdPath,
    status: Annotated[DeadlineStatus, Form()],
    session: Annotated[Session, Depends(get_session)],
    deadline_status: Annotated[str | None, Form(alias="current_status")] = None,
) -> HTMLResponse:
    """Apply a status change through the deadline endpoint and return the refreshed list."""
    error: str | None = None
    try:
        deadlines_api.update_deadline(
            deadline_id=deadline_id,
            payload=deadlines_api.DeadlineUpdate(status=status),
            session=session,
        )
    except HTTPException as exc:
        error = _error_text(exc.detail)
    return _deadlines_response(
        request,
        session,
        deadline_status=_deadline_status(deadline_status),
        error=error,
    )


# --------------------------------------------------------------------------- assistant


@router.get("/assistant", response_class=HTMLResponse, name="assistant_page")
def assistant_page(request: Request) -> HTMLResponse:
    """Question form; the disclaimer is part of the page, not only of the answers."""
    return _render(
        request,
        "assistant.html",
        page_title="Asystent",
        question="",
        answer=None,
        citations=(),
        disclaimer=DISCLAIMER,
        max_question_chars=assistant_api.MAX_QUESTION_CHARS,
        error=None,
    )


@router.post(
    "/assistant",
    response_class=HTMLResponse,
    name="assistant_ask",
    dependencies=[Depends(assistant_api._enforce_rate_limit)],
)
def ask_assistant_page(
    request: Request,
    question: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    answerer: Annotated[Answerer, Depends(assistant_api.get_answerer)],
    embedder: Annotated[Embedder, Depends(assistant_api.get_embedder)],
) -> HTMLResponse:
    """Ask through the assistant endpoint; answer, citations and disclaimer render together."""
    cleaned = question.strip()
    error: str | None = None
    reply: assistant_api.AssistantAskResponse | None = None
    if not cleaned:
        error = "Pytanie nie może być puste."
    else:
        try:
            reply = assistant_api.ask(
                payload=assistant_api.AssistantAskRequest(question=cleaned),
                session=session,
                settings=settings,
                answerer=answerer,
                embedder=embedder,
            )
        except HTTPException as exc:
            error = _error_text(exc.detail)
        except ValidationError:
            error = (
                "Pytanie musi mieć od 1 do "
                f"{assistant_api.MAX_QUESTION_CHARS} znaków."
            )
    return _render(
        request,
        "assistant.html",
        page_title="Asystent",
        question=cleaned,
        answer=reply.answer if reply is not None else None,
        citations=reply.citations if reply is not None else (),
        disclaimer=reply.disclaimer if reply is not None else DISCLAIMER,
        max_question_chars=assistant_api.MAX_QUESTION_CHARS,
        error=error,
    )
