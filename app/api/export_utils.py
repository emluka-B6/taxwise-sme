"""Shared helpers for the locally-generated export endpoints (JPK_PIT, FA(3)).

Both exports return ``application/*`` files that are never transmitted to a government
system; a validation failure is reported as ``422`` with the full violation list and a
boolean flag recording that nothing was submitted. This keeps the two endpoints
consistent instead of re-implementing the response shape.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException
from fastapi import status as http_status

from app.integrations.schemas import Violation

__all__ = ["unprocessable"]


def unprocessable(
    message: str,
    violations: Sequence[Violation],
    *,
    headers: dict[str, str],
    not_submitted_key: str,
) -> HTTPException:
    """A ``422`` carrying the full violation list and the "not transmitted" guarantee.

    ``not_submitted_key`` is the boolean flag name in the detail payload that records the
    document was generated locally (``submitted_to_mf`` or ``sent_to_ksef``).
    """
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": message,
            "violations": [violation.as_dict() for violation in violations],
            not_submitted_key: False,
        },
        headers=headers,
    )
