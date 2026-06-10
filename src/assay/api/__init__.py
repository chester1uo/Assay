"""Assay REST API package (architecture §4).

A FastAPI application exposing :class:`~assay.service.AssayService` over HTTP: factor
evaluation (blocking + SSE), batch, the factor library, sessions and system status.
The single public export is :data:`app`; importing it requires no credentials (the
data store is built lazily on first data-touching request — architecture §4.5).
"""

from __future__ import annotations

from assay.api.app import app, create_app

__all__ = ["app", "create_app"]
