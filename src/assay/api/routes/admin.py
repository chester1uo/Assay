"""Admin console — a deliberately *unlinked* operations page at ``GET /admin``.

Not part of the SPA and not in the top nav: you reach it by typing the URL. It is a
single server-rendered, old-school HTML page (gray background, bordered tables, a
``<meta http-equiv=refresh>`` auto-reload) showing data sources, system performance,
service info, and recent request status + logs — the stuff an operator wants, kept
out of the analyst UI.

Two in-memory ring buffers feed it: every HTTP request (method/path/status/latency)
and every WARNING+ log record. :func:`install` wires the request middleware and the
log handler onto the app and stamps the process start time; :mod:`assay.api.app`
calls it during ``create_app`` and includes :data:`router`.

House style: ``from __future__ import annotations``, stdlib-only (no psutil), Linux
``/proc`` for memory, best-effort everywhere (the page must render even with no data).
"""

from __future__ import annotations

import html
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from assay import __version__
from assay.api.app import get_service

router = APIRouter()

# Ring buffers (newest appended on the right). Bounded so memory is constant.
_REQUESTS: deque[dict[str, Any]] = deque(maxlen=600)
_LOGS: deque[dict[str, Any]] = deque(maxlen=400)
_COUNTERS = {"total": 0, "errors": 0}
_STARTED_AT = time.time()


# ---------------------------------------------------------------------------
# install: request middleware + log capture (called from create_app)
# ---------------------------------------------------------------------------
class _RingLogHandler(logging.Handler):
    """A logging handler that appends formatted records to the :data:`_LOGS` ring."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOGS.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass


def install(app) -> None:
    """Wire the request-log middleware + WARNING-level log capture onto ``app``."""
    global _STARTED_AT
    _STARTED_AT = time.time()

    @app.middleware("http")
    async def _record_request(request, call_next):
        # Skip the admin page's own (auto-refreshing) traffic so it doesn't flood.
        path = request.url.path
        if path.startswith("/admin"):
            return await call_next(request)
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            _COUNTERS["total"] += 1
            if status >= 500:
                _COUNTERS["errors"] += 1
            _REQUESTS.append({
                "ts": time.time(), "method": request.method, "path": path,
                "status": status, "ms": ms,
            })

    handler = _RingLogHandler(level=logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    # Avoid double-installing on app re-create (tests build many apps).
    if not any(isinstance(h, _RingLogHandler) for h in root.handlers):
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# metric gathering
# ---------------------------------------------------------------------------
def _proc_rss_mb() -> float | None:
    """Resident set size in MB from ``/proc/self/status`` (Linux); None elsewhere."""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _market_coverage(svc, market: str) -> dict[str, Any]:
    """Ingested span + data dir for one market's store (best-effort)."""
    cfg = svc._config_for_market(market)
    info: dict[str, Any] = {"data_dir": str(cfg.data_dir), "first": None, "last": None, "days": 0, "last_sync": None}
    try:
        import polars as pl

        from assay.data.schemas import price_root

        proot = price_root(cfg.data_dir, market)
        files = sorted(proot.glob("**/price_raw.parquet")) if proot.exists() else []
        if files:
            df = pl.read_parquet([str(f) for f in files], columns=["date", "as_of_date"])
            if df.height:
                info["days"] = int(df["date"].n_unique())
                info["first"] = str(df["date"].min())
                info["last"] = str(df["date"].max())
                info["last_sync"] = str(df["as_of_date"].max())
    except Exception:
        pass
    return info


_KNOWN_UNIVERSES = ("NASDAQ100", "SP500", "RUSSELL2000", "CSI300", "CSI500", "CSI1000")


def _gather(svc) -> dict[str, Any]:
    """Collect everything the admin page renders (every read guarded)."""
    cfg = svc.config
    # markets in play: the primary + any extra per-market data dirs configured.
    markets = []
    for m in [cfg.market, *sorted(getattr(cfg, "market_dirs", {}) or {})]:
        if m not in markets:
            markets.append(m)
    # universes grouped by their market
    uni_by_market: dict[str, list[dict[str, Any]]] = {}
    _start, end = cfg.default_period
    for uid in _KNOWN_UNIVERSES:
        mk = svc._market_for(uid)
        n = 0
        try:
            n = len(svc.store_for_universe(uid).get_universe(uid, end, end))
        except Exception:
            pass
        uni_by_market.setdefault(mk, []).append({"id": uid, "n_symbols": n})
    # ensure every universe-market is represented even if not a configured data dir
    for mk in uni_by_market:
        if mk not in markets:
            markets.append(mk)

    sources = []
    for mk in markets:
        cov = _market_coverage(svc, mk)
        sources.append({"market": mk, **cov, "universes": uni_by_market.get(mk, [])})

    # cache stats
    cache = {}
    try:
        cache = svc.cache.stats()
    except Exception:
        cache = {}

    reqs = list(_REQUESTS)
    recent = reqs[-200:][::-1]  # newest first
    lat = [r["ms"] for r in reqs[-200:]]
    avg_ms = (sum(lat) / len(lat)) if lat else 0.0

    return {
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "service": {
            "engine_version": __version__,
            "primary_market": cfg.market,
            "library_path": str(svc.library.path),
            "library_size": _safe(lambda: len(svc.library_query(limit=-1)), 0),
            "default_universe": cfg.default_universe,
            "default_period": " → ".join(cfg.default_period),
            "default_horizons": ", ".join(map(str, cfg.default_horizons)),
            "default_execution": cfg.default_execution,
            "default_adj": cfg.default_adj,
        },
        "perf": {
            "uptime": _fmt_uptime(time.time() - _STARTED_AT),
            "rss_mb": _proc_rss_mb(),
            "threads": threading.active_count(),
            "requests_total": _COUNTERS["total"],
            "requests_errors": _COUNTERS["errors"],
            "avg_latency_ms": avg_ms,
            "active_sessions": _safe(lambda: svc.sessions.active_sessions(), 0),
        },
        "cache": cache,
        "sources": sources,
        "requests": recent,
        "logs": list(_LOGS)[-120:][::-1],
    }


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# old-school HTML rendering
# ---------------------------------------------------------------------------
_CSS = """
body { background:#cfcfc4; color:#101010; font-family:Verdana,Geneva,"MS Sans Serif",sans-serif; font-size:12px; margin:0; padding:0 0 24px; }
a { color:#0000cc; } a:visited { color:#551a8b; }
.bar { background:#000080; color:#ffffff; padding:6px 10px; font-weight:bold; letter-spacing:1px; border-bottom:2px solid #000040; }
.bar small { color:#c0c0ff; font-weight:normal; letter-spacing:0; }
.wrap { padding:10px 14px; }
h2 { font-size:13px; color:#000080; border-bottom:1px solid #808080; margin:18px 0 6px; padding-bottom:2px; }
table { border-collapse:collapse; background:#ffffff; margin:4px 0; }
table.grid { border:2px solid #808080; }
th,td { border:1px solid #b0b0b0; padding:2px 7px; text-align:left; vertical-align:top; }
th { background:#000080; color:#ffffff; font-weight:bold; }
tr:nth-child(even) td { background:#eeeee6; }
.kv th { background:#d4d0c8; color:#000; text-align:right; white-space:nowrap; }
.mono, td.mono { font-family:"Courier New",monospace; }
.s2 { color:#006600; } .s3 { color:#0000aa; } .s4 { color:#aa6600; } .s5 { color:#cc0000; font-weight:bold; }
.lvl-WARNING { color:#aa6600; } .lvl-ERROR,.lvl-CRITICAL { color:#cc0000; font-weight:bold; }
.foot { color:#505050; font-size:11px; margin-top:18px; border-top:1px solid #808080; padding-top:6px; }
.muted { color:#707070; }
"""


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _kv_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(f"<tr><th>{_esc(k)}</th><td class='mono'>{_esc(v)}</td></tr>" for k, v in rows)
    return f"<table class='grid kv'>{body}</table>"


def _status_class(code: int) -> str:
    return f"s{code // 100}" if code else "s5"


def _render(d: dict[str, Any]) -> str:
    s = d["service"]
    p = d["perf"]
    rss = f"{p['rss_mb']:.1f} MB" if p["rss_mb"] is not None else "—"

    service_tbl = _kv_table([
        ("engine", s["engine_version"]),
        ("primary market", s["primary_market"]),
        ("library", f"{s['library_size']} factors"),
        ("library path", s["library_path"]),
        ("default universe", s["default_universe"]),
        ("default period", s["default_period"]),
        ("default horizons", s["default_horizons"]),
        ("execution / adj", f"{s['default_execution']} / {s['default_adj']}"),
    ])
    perf_tbl = _kv_table([
        ("uptime", p["uptime"]),
        ("memory (RSS)", rss),
        ("threads", p["threads"]),
        ("requests served", p["requests_total"]),
        ("5xx errors", p["requests_errors"]),
        ("avg latency (last 200)", f"{p['avg_latency_ms']:.1f} ms"),
        ("active sessions", p["active_sessions"]),
    ])
    c = d["cache"] or {}
    cache_tbl = _kv_table([
        ("L2 entries", c.get("entries", "—")),
        ("L2 size", f"{(c.get('bytes', 0) or 0) / 1e6:.1f} MB"),
        ("hit rate", c.get("hit_rate", "—")),
    ])

    # data sources
    src_rows = []
    for src in d["sources"]:
        unis = ", ".join(f"{u['id']}({u['n_symbols']})" for u in src["universes"]) or "—"
        span = f"{src['first'] or '—'} → {src['last'] or '—'}"
        src_rows.append(
            f"<tr><td><b>{_esc(src['market'])}</b></td>"
            f"<td class='mono'>{_esc(src['data_dir'])}</td>"
            f"<td class='mono'>{_esc(span)}</td>"
            f"<td>{src['days']}</td>"
            f"<td class='mono'>{_esc(src['last_sync'])}</td>"
            f"<td>{_esc(unis)}</td></tr>"
        )
    sources_tbl = (
        "<table class='grid'><tr><th>market</th><th>data dir</th><th>coverage</th>"
        "<th>days</th><th>last sync</th><th>universes (n_symbols)</th></tr>"
        + "".join(src_rows) + "</table>"
    )

    # requests
    req_rows = []
    for r in d["requests"]:
        t = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%H:%M:%S")
        cls = _status_class(int(r["status"]))
        req_rows.append(
            f"<tr><td class='mono'>{t}</td><td class='mono'>{_esc(r['method'])}</td>"
            f"<td class='mono'>{_esc(r['path'])}</td>"
            f"<td class='mono {cls}'>{r['status']}</td>"
            f"<td class='mono' align='right'>{r['ms']:.0f}</td></tr>"
        )
    requests_tbl = (
        "<table class='grid'><tr><th>time</th><th>method</th><th>path</th><th>status</th><th>ms</th></tr>"
        + ("".join(req_rows) or "<tr><td colspan=5 class='muted'>no requests yet</td></tr>")
        + "</table>"
    )

    # logs
    log_rows = []
    for lg in d["logs"]:
        t = datetime.fromtimestamp(lg["ts"], timezone.utc).strftime("%H:%M:%S")
        log_rows.append(
            f"<tr><td class='mono'>{t}</td>"
            f"<td class='mono lvl-{_esc(lg['level'])}'>{_esc(lg['level'])}</td>"
            f"<td class='mono'>{_esc(lg['name'])}</td>"
            f"<td class='mono'>{_esc(lg['msg'])}</td></tr>"
        )
    logs_tbl = (
        "<table class='grid'><tr><th>time</th><th>level</th><th>logger</th><th>message</th></tr>"
        + ("".join(log_rows) or "<tr><td colspan=4 class='muted'>no warnings/errors logged</td></tr>")
        + "</table>"
    )

    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<meta name="robots" content="noindex,nofollow">
<title>Assay :: Admin</title>
<style>{_CSS}</style>
</head>
<body>
<div class="bar">ASSAY ADMIN CONSOLE &nbsp; <small>operations &middot; not linked from the app</small></div>
<div class="wrap">
<h2>Service</h2>{service_tbl}
<h2>System performance</h2>{perf_tbl}
<h2>L2 factor cache</h2>{cache_tbl}
<h2>Data sources</h2>{sources_tbl}
<h2>Recent requests ({len(d['requests'])})</h2>{requests_tbl}
<h2>Recent log (WARNING+)</h2>{logs_tbl}
<div class="foot">generated {_esc(d['now'])} &middot; auto-refresh every 10s &middot;
<a href="/admin">reload now</a> &middot; <a href="/admin/data.json">raw JSON</a> &middot;
<a href="/#/data">data manager &raquo;</a></div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page() -> HTMLResponse:
    """Server-rendered, auto-refreshing operations console (old-school HTML)."""
    return HTMLResponse(_render(_gather(get_service())))


@router.get("/admin/data.json", include_in_schema=False)
def admin_data() -> dict[str, Any]:
    """The same metrics as a JSON blob (for scripts / scraping)."""
    return _gather(get_service())
