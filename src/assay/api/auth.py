"""API-key auth dependency (architecture §4.1).

The REST API authenticates with an ``X-API-Key`` header (or a ``Bearer`` token for
WebUI sessions). The allowed keys come from the ``ASSAY_API_KEYS`` environment
variable as a comma-separated list. **When the variable is unset (or empty) auth is
DISABLED and every request is allowed** — the offline / single-user default so the
app boots and the test client works without credentials.

Usage in a route::

    @router.post("/evaluate")
    async def evaluate(req: EvaluateRequest, api_key: str | None = Depends(get_api_key)):
        ...

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException, status

__all__ = ["get_api_key", "allowed_keys", "auth_enabled"]


def allowed_keys() -> set[str]:
    """Parse ``ASSAY_API_KEYS`` (comma-separated) into a set; empty set => auth off.

    Read on every call (not cached) so tests / runtime can toggle the env var without
    re-importing the module. Blank entries and surrounding whitespace are ignored.
    """
    raw = os.environ.get("ASSAY_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def auth_enabled() -> bool:
    """True when at least one key is configured (i.e. auth should be enforced)."""
    return bool(allowed_keys())


async def get_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str | None:
    """FastAPI dependency: validate the request's API key, or allow all when auth is off.

    Resolution order: the ``X-API-Key`` header, then a ``Bearer <token>`` in
    ``Authorization`` (WebUI sessions, architecture §4.1). When no keys are
    configured the dependency is a no-op and returns ``None`` (auth disabled).
    Otherwise a missing/unknown key raises ``HTTP 401``.
    """
    keys = allowed_keys()
    if not keys:  # auth disabled — allow every caller
        return None

    presented = x_api_key
    if presented is None and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            presented = token.strip()

    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key (set the X-API-Key header).",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    if presented not in keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return presented
