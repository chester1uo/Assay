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
    """Merge ``patch`` into the config (secrets kept unless a fresh value is sent)."""
    config_store.update(patch or {})
    return config_store.masked()


# ------------------------------------------------------------------ status
@router.get("/v1/admin/data/status", include_in_schema=False)
def data_status(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """Per-market RAW-vs-ASSAY sync snapshot (latest dates, days behind, in-sync)."""
    return orchestrate.status()


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
    """Queue an init/update pipeline job. Body: ``{market, mode, start?, end?}``.

    ``market`` ∈ US|CN; ``mode`` ∈ init|update. When ``start``/``end`` are omitted they
    default to the full history (init) or the incremental range since the last ingest
    (update). Returns the queued job (no logs).
    """
    market = str(body.get("market", "")).upper()
    mode = str(body.get("mode", "update")).lower()
    if market not in ("US", "CN"):
        raise HTTPException(status_code=422, detail="market must be US or CN")
    if mode not in ("init", "update"):
        raise HTTPException(status_code=422, detail="mode must be init or update")

    d_start, d_end = orchestrate.default_range(market, mode)
    start = _parse_date(body.get("start")) or d_start
    end = _parse_date(body.get("end")) or d_end
    if start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")

    label = f"{mode} {start.isoformat()}..{end.isoformat()}"
    job = jobs.submit(mode, market, label, lambda j: orchestrate.run(market, mode, start, end, j))
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
