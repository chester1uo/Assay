"""Public endpoint configuration shared by REST discovery and MCP transport."""
from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit
from fastapi import Request

INTERFACE_VERSION = "2026-11-14.1"


def _origin_aliases(origin):
    parts = urlsplit(origin)
    if parts.port is None:
        return (origin, f"{origin}:{443 if parts.scheme == 'https' else 80}")
    return (origin,)


def endpoint_origin(value: str) -> str:
    parts = urlsplit(value.strip())
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.path not in {"", "/"}
            or parts.query or parts.fragment):
        raise ValueError("ASSAY_PUBLIC_URL must be an HTTP(S) origin without credentials, path or query")
    # Accessing port also rejects malformed port numbers.
    port = parts.port
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port and port != {"http": 80, "https": 443}[parts.scheme] else ""
    return f"{parts.scheme}://{host}{suffix}"


@dataclass(frozen=True)
class DeploymentSettings:
    public_url: str | None
    production: bool
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls):
        mode = os.environ.get("ASSAY_DEPLOYMENT", "development").strip().lower()
        if mode not in {"development", "production"}:
            raise ValueError("ASSAY_DEPLOYMENT must be development or production")
        raw = os.environ.get("ASSAY_PUBLIC_URL", "").strip()
        public = endpoint_origin(raw) if raw else None
        origins = tuple(endpoint_origin(o) for o in os.environ.get("ASSAY_CORS_ORIGINS", "").split(",") if o.strip())
        production = mode == "production"
        if production:
            from assay.api.auth import auth_enabled
            if not public or not public.startswith("https://"):
                raise ValueError("production requires an HTTPS ASSAY_PUBLIC_URL")
            if not auth_enabled():
                raise ValueError("production requires ASSAY_ACCOUNTS=1 or ASSAY_API_KEYS")
            if os.environ.get("ASSAY_MCP_HTTP", "1") == "0":
                raise ValueError("production requires the shared MCP HTTP endpoint")
        origins = origins or ((public,) if public else ())
        return cls(public, production, tuple(dict.fromkeys(alias for o in origins for alias in _origin_aliases(o))))

    def mcp_security(self):
        from mcp.server.transport_security import TransportSecuritySettings

        hosts = ["127.0.0.1:*", "127.0.0.1", "localhost:*", "localhost", "[::1]:*", "[::1]"]
        origins = list(self.cors_origins)
        if self.public_url:
            for alias in _origin_aliases(self.public_url):
                hosts.append(urlsplit(alias).netloc)
                origins.append(alias)
        if not self.production:
            hosts.append("testserver")
            origins += ["http://localhost:*", "http://localhost", "http://127.0.0.1:*", "http://127.0.0.1",
                        "http://[::1]:*", "http://[::1]", "http://testserver", "https://testserver"]
        return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=hosts, allowed_origins=list(dict.fromkeys(origins)))


def install_endpoints(app, settings, mcp_app, get_service):
    from fastapi import Depends
    from fastapi.responses import JSONResponse
    from assay.api.auth import accounts_enabled, auth_enabled, get_api_key

    @app.get("/.well-known/assay.json", tags=["System"])
    async def discovery(request: Request):
        """Assay-specific connection metadata; not OAuth authorization metadata."""
        import hashlib
        import json
        from assay import __version__

        base = settings.public_url or str(request.base_url).rstrip("/")
        tools = []
        if mcp_app is not None:
            from assay.mcp.server import mcp
            tools = await mcp.list_tools()
        schemas = [t.model_dump(mode="json", exclude_none=True) for t in tools]
        return dict(name="Assay", version=__version__, interface_version=INTERFACE_VERSION,
                    base_url=base, rest_url=base+"/v1", openapi_url=base+"/openapi.json",
                    docs_url=base+"/doc/", readiness_url=base+"/ready",
                    mcp=dict(url=base+"/mcp/", transport="streamable-http", available=mcp_app is not None,
                             tool_count=len(tools), schema_sha256=hashlib.sha256(
                                 json.dumps(schemas, sort_keys=True).encode()).hexdigest()),
                    authentication=dict(required=auth_enabled(), methods=["bearer", "x-api-key"],
                                        oauth=False, personal_keys_url=base+"/login"),
                    research=dict(persistent=True, scope=("account-workspace" if accounts_enabled() else
                                  "service-workspace" if auth_enabled() else "local-shared-workspace"),
                                  tenant_isolation=accounts_enabled(),
                                  service_key_scope="shared-service-workspace",
                                  library_scope="account" if accounts_enabled() else "local-shared",
                                  panel_session_scope="process", panel_sessions_require_sticky_routing=True,
                                  shared_compute_registry=True, account_shared_compute_access=False,
                                  private_compute_registry=accounts_enabled(), private_compute_requires_ades=True,
                                  private_compute_configured=bool(accounts_enabled() and
                                      os.environ.get('ASSAY_FACTORDB_DIR') and os.environ.get('ASSAY_QFV_PANEL_DIR') and
                                      os.environ.get('ASSAY_ADES', '').lower() in ('1', 'true', 'yes'))))

    @app.get("/ready", tags=["System"], dependencies=[Depends(get_api_key)])
    def ready():
        """Control-plane readiness: writable research storage and MCP transport.

        Market coverage and a particular experiment's executability are separate.
        """
        checks = {"research_store": False, "mcp_http": mcp_app is not None}
        try:
            ws = get_service().research
            with ws._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE datasets SET payload=payload WHERE 0")
                db.rollback()
            checks["research_store"] = True
        except Exception:
            pass
        ok = all(checks.values())
        return JSONResponse(dict(status="ready" if ok else "not_ready", checks=checks,
                                 interface_version=INTERFACE_VERSION, market_coverage_checked=False),
                            status_code=200 if ok else 503)

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes.update(AssayBearer={"type": "http", "scheme": "bearer"},
                       AssayApiKey={"type": "apiKey", "in": "header", "name": "X-API-Key"})
        for path, operations in schema.get("paths", {}).items():
            if path.startswith("/v1/") or path == "/ready":
                for method, operation in operations.items():
                    if method in {"get", "post", "put", "patch", "delete"}:
                        operation["security"] = [{"AssayBearer": []}, {"AssayApiKey": []}]
        return schema
    app.openapi = openapi
