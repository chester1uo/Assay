"""Data-manager routes — editable config + RAW→ASSAY status + ingest jobs.

Operator/admin surface (reached from the WebUI data manager, not the analyst nav):

    GET  /v1/admin/config            -> current config (secrets MASKED)
    PUT  /v1/admin/config            -> update config (dirs / S3 creds / Tushare token)
    GET  /v1/admin/data/status       -> per-market RAW vs ASSAY sync snapshot
    POST /v1/admin/data/jobs         -> start an init/update pipeline job for a market
    GET  /v1/admin/data/jobs         -> list jobs (compact, no logs)
    GET  /v1/admin/data/jobs/{id}    -> one job with its log

Heavy disk/ingest work runs in the background :mod:`assay.data.jobs` queue (one at a
time) so the event loop never blocks. Secrets are masked on read and preserved on
write unless a fresh value is supplied (see :mod:`assay.config_store`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from assay import config_store
from assay.api.auth import get_api_key
from assay.data import jobs, orchestrate

router = APIRouter()


# ------------------------------------------------------------------ config
@router.get("/v1/admin/config", include_in_schema=False)
def get_config(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Current config with secret leaves masked to ``••••last4``."""
    return config_store.masked()


@router.put("/v1/admin/config", include_in_schema=False)
def put_config(patch: dict, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Merge ``patch`` into the config (secrets kept unless a fresh value is sent).

    Data settings (dirs / credentials) are pushed to the environment; **system
    settings** (parallelism / cache / evaluation defaults) are applied onto the live
    service so they take effect on the next request without a restart.
    """
    config_store.update(patch or {})
    if isinstance(patch, dict) and "system" in patch:
        try:
            from assay.api.app import get_service

            get_service().apply_system_config()
        except Exception:  # noqa: BLE001 — service may be uninitialised in some contexts
            pass
    return config_store.masked()


# ------------------------------------------------------------------ status
@router.get("/v1/admin/data/status", include_in_schema=False)
def data_status(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Per-market RAW-vs-ASSAY sync snapshot (latest dates, days behind, in-sync)."""
    return orchestrate.status()


@router.get("/v1/admin/data/usage", include_in_schema=False)
def data_usage(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Per-market on-disk footprint of the prepared ASSAY stores (size + breakdown)."""
    return orchestrate.usage()


# ------------------------------------------------------------------ test connection
@router.post("/v1/admin/data/test", include_in_schema=False)
def test_connection(body: dict, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Test a provider's saved credentials. Body: ``{provider}`` (massive|tushare)."""
    from assay.data import testconn

    provider = str((body or {}).get("provider", "")).lower()
    if provider not in ("massive", "tushare"):
        raise HTTPException(status_code=422, detail="provider must be 'massive' or 'tushare'")
    return testconn.test(provider)


# ------------------------------------------------------------------ auto-update schedule
@router.get("/v1/admin/schedule", include_in_schema=False)
def get_schedule(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Auto-update schedule state (per market: enabled / time / last / next run)."""
    from assay.data import scheduler

    return scheduler.status()


@router.put("/v1/admin/schedule", include_in_schema=False)
def put_schedule(patch: dict, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Update the schedule. Body: ``{schedule: {us:{enabled,time}, cn:{...}}}`` (or flat)."""
    from assay.data import scheduler

    sched = (patch or {}).get("schedule", patch or {})
    config_store.update({"schedule": sched})
    scheduler.start()  # ensure the daemon is running once a schedule is set
    return scheduler.status()


# ------------------------------------------------------------------ jobs
def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


@router.post("/v1/admin/data/jobs", include_in_schema=False)
def start_job(body: dict, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Queue a data pipeline job. Body: ``{market, mode, start?, end?}``.

    ``market`` ∈ US|CN; ``mode`` ∈ init|update|**ingest**. ``ingest`` skips the download
    and only re-runs RAW→ASSAY (for when raw is already present). When ``start``/``end``
    are omitted they default to the full history (init), the incremental range since the
    last ingest (update), or last-ASSAY→latest-RAW (ingest). Returns the queued job.
    """
    market = str(body.get("market", "")).upper()
    mode = str(body.get("mode", "update")).lower()
    if market not in ("US", "CN"):
        raise HTTPException(status_code=422, detail="market must be US or CN")
    if mode not in ("init", "update", "ingest"):
        raise HTTPException(status_code=422, detail="mode must be init, update or ingest")

    d_start, d_end = orchestrate.default_range(market, mode)
    start = _parse_date(body.get("start")) or d_start
    end = _parse_date(body.get("end")) or d_end
    if start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")

    label = f"{mode} {start.isoformat()}..{end.isoformat()}"

    def _task(j):
        # 1) ingest, then 2) refresh the hot cache (precompute) so it stays aligned
        #    to the freshly-ingested data validity period — automatically.
        rep = orchestrate.run(market, mode, start, end, j)
        try:
            from assay.api.app import get_service

            svc = get_service()
            if not getattr(svc.config, "precompute_auto_refresh", True):
                j.log("hot-cache auto-refresh disabled in system settings — skipped")
                return rep
            j.log("refreshing hot cache (precompute) to align with new data …")
            cache = svc.refresh_precompute_for_market(
                market,
                universes=None if mode == "init" else _AUTO_REFRESH_UNIVERSES.get(market),
                progress=lambda f, m: j.progress_to(1.0, m),  # keep the bar full, update msg
            )
            j.log(f"hot cache refreshed: {cache.get('refreshed') or cache.get('note')}")
            rep = {**(rep or {}), "precompute": cache}
        except Exception as exc:  # noqa: BLE001 — a cache hiccup must not fail the ingest
            j.log(f"hot-cache refresh skipped: {type(exc).__name__}: {exc}")
        return rep

    job = jobs.submit(mode, market, label, _task)
    return job.to_dict(with_logs=False)


# Auto-refresh scope after an *update* run: just the primary universe per market
# (fast, keeps the job snappy). An ``init`` run refreshes every universe (None).
_AUTO_REFRESH_UNIVERSES = {"US": ["NASDAQ100"], "CN": ["CSI300"]}


# ------------------------------------------------------------------ hot cache
@router.get("/v1/admin/cache/status", include_in_schema=False)
def cache_status(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Hot-cache (precompute) status: footprint + per-universe validity & freshness."""
    from assay.api.app import get_service

    return get_service().precompute_status()


@router.get("/v1/admin/cache/entries", include_in_schema=False)
def cache_entries(scope: str, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Detailed contents of one cache scope (e.g. ``?scope=NASDAQ100``).

    Lists every precomputed sub-expression with its expression, occurrence count,
    node count, recompute-saved score, cached matrix shape, bytes, coverage and
    whether it is still present for the scope's current fingerprint.
    """
    from assay.api.app import get_service

    return get_service().cache_entries(scope)


@router.post("/v1/admin/cache/rebuild", include_in_schema=False)
def cache_rebuild(body: dict | None = None, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Queue a hot-cache rebuild job. Body: ``{market}`` (US|CN; default US).

    Mines the factor library's common sub-expressions and precomputes them for the
    market's universes over the current data range (all universes — the thorough
    rebuild the data-update hook skips for speed).
    """
    market = str((body or {}).get("market", "US")).upper()
    if market not in ("US", "CN", "HK"):
        raise HTTPException(status_code=422, detail="market must be US, CN or HK")

    def _task(j):
        from assay.api.app import get_service

        return get_service().refresh_precompute_for_market(
            market, progress=lambda f, m: j.progress_to(f, m)
        )

    job = jobs.submit("cache", market, f"rebuild hot cache ({market})", _task)
    return job.to_dict(with_logs=False)


@router.get("/v1/admin/data/jobs", include_in_schema=False)
def list_jobs(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """All known jobs, newest first (compact, no logs)."""
    return {"jobs": [j.to_dict(with_logs=False) for j in jobs.list_jobs()]}


@router.get("/v1/admin/data/jobs/{job_id}", include_in_schema=False)
def get_job(job_id: str, api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """One job with its rolling log (for the progress view)."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return job.to_dict(with_logs=True)
