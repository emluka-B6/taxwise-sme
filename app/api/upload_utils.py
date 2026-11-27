"""Shared multipart-upload reading for the API layer.

Every endpoint that accepts a file applies the same size-cap / empty-file policy, so the
buffer-then-verify logic lives here instead of being duplicated per router.
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile
from fastapi import status as http_status

__all__ = ["read_upload"]


def read_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    """Read ``upload``, refusing anything above ``max_bytes`` before buffering it."""
    too_large = f"Files larger than {max_bytes // (1024 * 1024)} MB are rejected."
    declared = upload.size
    if declared is not None and declared > max_bytes:
        raise HTTPException(status_code=413, detail=too_large)  # Content Too Large
    data = upload.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=too_large)  # Content Too Large
    if not data:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty.",
        )
    return data
