"""Session routes (architecture §4.2).

``POST /v1/session/create`` pre-loads a data panel and returns its descriptor; later
``/evaluate`` / ``/batch`` calls carrying the ``session_id`` (or ``X-Session-Id``
header) skip the panel load and run the hot path only. ``DELETE /v1/session/{id}``
releases the session and its matrices.

Creating a session loads data, so it may surface HTTP 503 when no data is ingested.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from assay.api.app import get_service
from assay.api.auth import get_api_key
from assay.api.models import SessionCreateRequest

router = APIRouter()


@router.post("/create")
async def create_session(
    req: SessionCreateRequest,
    api_key: str | None = Depends(get_api_key),
) -> dict:
    """Create a panel-preloading session -> ``{session_id, setup_ms, ...}`` (architecture §4.2)."""
    svc = get_service()
    return svc.create_session(**req.service_kwargs())


@router.delete("/{session_id}")
async def expire_session(
    session_id: str,
    api_key: str | None = Depends(get_api_key),
) -> dict:
    """Release a session and its cached matrices -> ``{session_id, expired}``."""
    svc = get_service()
    expired = svc.expire_session(session_id)
    return {"session_id": session_id, "expired": expired}
