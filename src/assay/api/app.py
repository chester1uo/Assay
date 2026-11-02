"""FastAPI application wiring (architecture §4.5).

Builds the ``Assay API`` app: CORS allow-all, the four ``/v1`` routers
(``factor`` / ``library`` / ``session`` / ``system``), a lazy :class:`AssayService`
accessor, and the §4.6 error envelope for engine / validation / transport failures.

**Importing this module requires no credentials.** The service is *not* built at
import; :func:`get_service` initialises it lazily on first request (from the env /
project ``.env`` via :func:`assay.init`). Anything that needs the data store (panel
loads, sessions) only fails when actually called, and those failures are mapped to
HTTP 503 by :func:`service_unavailable_handler` so a credential-less deployment still
serves the library / status surfaces.

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

import os

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import assay
from assay.engine.engine import EvaluationError
from assay.engine.parsing import ParseError
from assay.service import AssayService

__all__ = ["app", "create_app", "get_service"]

logger = logging.getLogger("assay.api")


# --------------------------------------------------------------- service dep --
def get_service() -> AssayService:
    """Return the live :class:`AssayService`, lazily initialising from the env.

    Used as a plain call (not a FastAPI ``Depends``) inside every route so the data
    store stays unbuilt until a request actually needs it. Initialisation reads the
    environment / project ``.env`` (:func:`assay.init`); credential errors only fire
    when a data-touching method is invoked, and surface as HTTP 503.
    """
    try:
        return AssayService.get()
    except RuntimeError:
        # Not yet initialised — build from env (offline-safe; data store is lazy).
        return assay.init()


# ----------------------------------------------------------- error envelope ---
def _error_payload(
    *,
    code: str,
    name: str,
    message: str,
    failure_mode: str | None = None,
    severity: str = "error",
    stage: str | None = None,
    location: dict[str, Any] | None = None,
    suggestion: str | None = None,
    factor_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the §4.6 ``{"error": {...}}`` envelope (drops null optionals)."""
    body: dict[str, Any] = {
        "code": code,
        "name": name,
        "failure_mode": failure_mode,
        "severity": severity,
        "stage": stage,
        "message": message,
        "location": location,
        "suggestion": suggestion,
        "factor_id": factor_id,
    }
    return {"error": {k: v for k, v in body.items() if v is not None}}


def _is_data_unavailable(exc: Exception) -> bool:
    """Heuristic: does this exception mean 'data store / credentials not available'?

    The data layer raises ``FileNotFoundError`` for un-ingested partitions and
    ``RuntimeError`` for missing MASSIVE creds; both should be 503, not 500.
    """
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    msg = str(exc).lower()
    needles = ("massive", "credential", "ingest", "no price", "no universe", "not initialized")
    return any(n in msg for n in needles)


def register_exception_handlers(app: FastAPI) -> None:
    """Map engine / validation / transport errors onto the §4.6 error schema."""

    @app.exception_handler(ParseError)
    async def parse_error_handler(request: Request, exc: ParseError) -> JSONResponse:
        # ParseError carries a diagnostics code-name + char span (architecture §4.6).
        location = None
        span = getattr(exc, "span", None)
        if span is not None:
            location = {"start": span[0], "end": span[1]}
        return JSONResponse(
            status_code=400,
            content=_error_payload(
                code="ASSAY-P001",
                name=getattr(exc, "code", "UNEXPECTED_TOKEN"),
                failure_mode="SYNTAX_ERROR",
                stage="parse",
                message=str(exc),
                location=location,
                suggestion="Check the expression against the operator schema.",
            ),
        )

    @app.exception_handler(EvaluationError)
    async def eval_error_handler(request: Request, exc: EvaluationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_payload(
                code="ASSAY-E001",
                name="RUNTIME_ERROR",
                failure_mode="RUNTIME_ERROR",
                stage="execute",
                message=str(exc),
                suggestion=f"Operator {getattr(exc, 'op', None)!r} failed; check its arguments.",
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                code="ASSAY-P000",
                name="INVALID_REQUEST",
                failure_mode="SYNTAX_ERROR",
                stage="parse",
                severity="error",
                message="Request body failed validation.",
                location={"errors": exc.errors()},
                suggestion="Fix the request fields and resubmit.",
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Preserve explicit HTTP errors (404/401/...) in the §4.6 envelope shape.
        name = {401: "UNAUTHORIZED", 404: "NOT_FOUND", 422: "INVALID_REQUEST"}.get(
            exc.status_code, "HTTP_ERROR"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                code=f"ASSAY-H{exc.status_code:03d}",
                name=name,
                message=str(exc.detail),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Data-store / credential failures -> 503; everything else -> 500.
        if _is_data_unavailable(exc):
            return JSONResponse(
                status_code=503,
                content=_error_payload(
                    code="ASSAY-S503",
                    name="DATA_NOT_FOUND",
                    message=str(exc),
                    suggestion="Ingest data / configure MASSIVE credentials, then retry.",
                ),
            )
        logger.exception("unhandled API error")
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                code="ASSAY-S500",
                name="INTERNAL_ERROR",
                message=str(exc) or exc.__class__.__name__,
            ),
        )


# ------------------------------------------------------------- app factory ----
def create_app() -> FastAPI:
    """Construct the FastAPI app (architecture §4.5). Importing never needs creds."""
    # Apply the editable runtime config (dirs + provider creds) to the environment
    # FIRST, so AssayService.from_env / the ingest pipeline see the operator's values.
    try:
        from assay import config_store

        config_store.apply_to_env()
    except Exception:  # pragma: no cover - config is best-effort
        pass

    # Start the in-process auto-update scheduler (a no-op unless a market is enabled
    # in the schedule config). Best-effort — never block app construction.
    try:
        from assay.data import scheduler

        scheduler.start()
    except Exception:  # pragma: no cover
        pass

    mcp_app, mcp_lifespan = _mcp_http()
    app = FastAPI(title="Assay API", version=assay.__version__, lifespan=mcp_lifespan)

    # ASSAY_CORS_ORIGINS: comma-separated origins allowed to call the API from a
    # browser (default: any origin, without credentials).
    origins = [o.strip() for o in os.environ.get("ASSAY_CORS_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=bool(origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    # Routers import `get_service` from this module, so import them here (after the
    # accessor + handlers are defined) to keep a clean one-way dependency.
    from assay.api.routes import (
        account, admin, ades, combination, data_admin, factor, fdb, legacy, library, market, portfolio,
        session, system,
    )

    app.include_router(account.router, tags=["Accounts"])

    app.include_router(factor.router, prefix="/v1/factor", tags=["Factor"])
    app.include_router(library.router, prefix="/v1/library", tags=["Library"])
    app.include_router(combination.router, prefix="/v1/combination", tags=["Combination"])
    app.include_router(market.router, prefix="/v1/market", tags=["Market"])
    app.include_router(portfolio.router, prefix="/v1/portfolio", tags=["Portfolio"])
    app.include_router(session.router, prefix="/v1/session", tags=["Session"])
    app.include_router(system.router, prefix="/v1/system", tags=["System"])
    app.include_router(fdb.router, prefix="/v1/fdb", tags=["FactorDB"])
    app.include_router(ades.router, prefix="/v1/ades", tags=["ADES"])
    # Unlinked operations console at GET /admin (also installs the request-log
    # middleware + WARNING-level log capture it renders). Reached by URL only.
    from fastapi import Depends

    from assay.api.auth import require_admin

    app.include_router(admin.router, tags=["Admin"], dependencies=[Depends(require_admin)])
    app.include_router(data_admin.router, tags=["Admin"], dependencies=[Depends(require_admin)])
    admin.install(app)
    # Easter egg: the same SPA in a 2000s skin at GET /legacy (URL only).
    app.include_router(legacy.router, tags=["Legacy"])

    @app.get("/health", tags=["System"])
    async def health() -> dict:
        """Liveness probe — always cheap, never touches data."""
        from assay.api.build import build_info

        return {"status": "ok", "engine_version": assay.__version__, "build": build_info()}

    # WebUI: serve the zero-build static shell at "/". Mounted LAST so the explicit
    # ``/v1/*`` routers and ``/health`` still resolve first; ``html=True`` serves
    # ``index.html`` for "/" and is the SPA fallback. Path is resolved relative to
    # this file so it works from any CWD. Skipped if the dir is absent (e.g. a
    # backend-only checkout) so importing the app never fails.
    static_dir = Path(__file__).resolve().parent / "static"
    _mount_doc_hub(app, static_dir / "doc")
    if mcp_app is not None:
        app.mount("/mcp", _McpAuth(mcp_app))

    @app.get("/login", include_in_schema=False)
    async def login_page():
        """Sign in, sign up with an invitation (``?invite=``), reset (``?reset=``), first setup."""
        from fastapi.responses import FileResponse

        return FileResponse(static_dir / "login.html", headers={"Cache-Control": "no-cache"})
    if static_dir.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=static_dir, html=True), name="webui")

    return app


def _mcp_http():
    """The MCP server as a streamable-HTTP ASGI app for ``/mcp/`` (or ``(None, None)``).

    Stateless (no per-client session state), so any worker can answer any call. The
    host check of the MCP SDK is off: the app sits behind the reverse proxy and every
    call is authenticated by :class:`_McpAuth`. ``ASSAY_MCP_HTTP=0`` turns it off."""
    if os.environ.get("ASSAY_MCP_HTTP", "1") == "0":
        return None, None
    try:
        from contextlib import asynccontextmanager

        from mcp.server.transport_security import TransportSecuritySettings

        from assay.mcp.server import mcp
    except Exception as exc:  # noqa: BLE001 — the API runs without the optional mcp extra
        logger.info("MCP over HTTP is off (%s)", exc)
        return None, None
    mcp.settings.streamable_http_path = "/"
    mcp.settings.stateless_http = True
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    asgi = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    return asgi, lifespan


class _McpAuth:
    """Authenticate ``/mcp/`` calls like the REST API (session cookie, personal or service key)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from assay.api.auth import _presented, auth_enabled, resolve_user

            request = Request(scope, receive)
            user = None
            if auth_enabled():
                try:
                    user = resolve_user(request, _presented(request.headers.get("x-api-key"),
                                                            request.headers.get("authorization")))
                except StarletteHTTPException:
                    user = None
                if user is None:
                    resp = JSONResponse({"error": "sign in or send an API key (X-API-Key or Bearer)"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                    await resp(scope, receive, send)
                    return
            scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


def _mount_doc_hub(app: FastAPI, doc_dir: Path) -> None:
    """Doc Hub at ``/doc/``: its own files, else its page shell (paths like ``/doc/guide/cli``).

    The pages are pre-rendered from ``docs/`` by ``scripts/build_doc_hub.py``.
    """
    from fastapi.responses import FileResponse, RedirectResponse

    root = doc_dir.resolve()
    shell = root / "index.html"
    headers = {"Cache-Control": "no-cache"}

    @app.get("/doc", include_in_schema=False)
    async def doc_root() -> RedirectResponse:
        return RedirectResponse("/doc/", status_code=308)

    @app.get("/doc/{path:path}", include_in_schema=False)
    async def doc_page(path: str):
        target = (root / path).resolve()
        if path and target.is_file() and root in target.parents:
            return FileResponse(target, headers=headers)
        if Path(path).suffix and Path(path).suffix != ".html":
            raise StarletteHTTPException(status_code=404)
        if not shell.is_file():
            raise StarletteHTTPException(status_code=404)
        return FileResponse(shell, headers=headers)


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that asks browsers to revalidate every asset before use.

    The default StaticFiles emits ``etag``/``last-modified`` but no
    ``cache-control``, so browsers may serve a stale cached copy without
    revalidating — which silently hands users an old WebUI bundle after a
    frontend fix. ``no-cache`` forces a conditional request each load; the
    etag still yields a cheap ``304`` when nothing changed.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app = create_app()
