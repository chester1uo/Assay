"""In-process auto-update scheduler for the Data Manager.

Assay's API is a single long-running uvicorn process, so a lightweight daemon
thread is the natural fit for "auto update": when a market is enabled in the
:mod:`assay.config_store` ``schedule`` section and its daily ``time`` arrives, the
scheduler enqueues an incremental *update* job (the same pipeline the Data Setup
buttons run) — never overlapping a job that is already running.

Everything is best-effort: a bad time string or a transient error is swallowed so
the thread never dies. :func:`status` powers the UI (enabled / time / last / next).
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Optional

from assay import config_store
from assay.data import jobs, orchestrate

_MARKETS = ("US", "CN")
_LOCK = threading.Lock()
_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()
_LAST_RUN: dict[str, dt.date] = {}   # market -> date the auto-update last fired


def _sched_for(market: str) -> dict:
    return dict(config_store.schedule().get(market.lower(), {}) or {})


def _parse_hhmm(s: str | None) -> Optional[tuple[int, int]]:
    try:
        h, m = str(s or "").split(":", 1)
        h, m = int(h), int(m)
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, TypeError):
        return None


def _any_job_running() -> bool:
    return any(j.status == "running" for j in jobs.list_jobs())


def _due(now: dt.datetime, market: str) -> bool:
    sc = _sched_for(market)
    if not sc.get("enabled"):
        return False
    hm = _parse_hhmm(sc.get("time"))
    if hm is None or _LAST_RUN.get(market) == now.date():
        return False
    return (now.hour, now.minute) >= hm


def _enqueue_update(market: str) -> None:
    d_start, d_end = orchestrate.default_range(market, "update")
    label = f"auto update {d_start.isoformat()}..{d_end.isoformat()}"

    def _task(job):
        rep = orchestrate.run(market, "update", d_start, d_end, job)
        try:  # best-effort hot-cache refresh, mirroring the manual update hook
            from assay.api.app import get_service

            svc = get_service()
            if getattr(svc.config, "precompute_auto_refresh", True):
                job.log("refreshing hot cache (precompute) …")
                svc.refresh_precompute_for_market(
                    market, universes=None, progress=lambda f, m: job.progress_to(1.0, m)
                )
        except Exception as exc:  # noqa: BLE001
            job.log(f"hot-cache refresh skipped: {type(exc).__name__}: {exc}")
        return rep

    jobs.submit("update", market, label, _task)


def _loop() -> None:
    # wake every 30s; fire at most one market per tick (single-worker job queue)
    while not _STOP.wait(30):
        try:
            now = dt.datetime.now()
            for mk in _MARKETS:
                if _due(now, mk) and not _any_job_running():
                    _LAST_RUN[mk] = now.date()
                    _enqueue_update(mk)
                    break
        except Exception:  # noqa: BLE001 — never let the thread die
            pass


def start() -> None:
    """Start the scheduler daemon once (idempotent)."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="assay-scheduler", daemon=True)
        _THREAD.start()


def status() -> dict[str, Any]:
    """Per-market schedule state for the UI: enabled / time / last / next run."""
    now = dt.datetime.now()
    out = []
    for mk in _MARKETS:
        sc = _sched_for(mk)
        hm = _parse_hhmm(sc.get("time"))
        nxt = None
        if sc.get("enabled") and hm:
            cand = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if cand <= now and _LAST_RUN.get(mk) == now.date():
                cand += dt.timedelta(days=1)
            nxt = cand.isoformat(timespec="minutes")
        lr = _LAST_RUN.get(mk)
        out.append({
            "market": mk, "enabled": bool(sc.get("enabled")), "time": sc.get("time"),
            "last_run": lr.isoformat() if lr else None, "next_run": nxt,
        })
    return {"markets": out, "running": _any_job_running(),
            "thread_alive": bool(_THREAD and _THREAD.is_alive())}
