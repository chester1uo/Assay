"""``/v1/ades/*``: the controller side of ADES, called by workers (see :mod:`assay.ades`).

Workers authenticate with a key from ``ASSAY_WORKER_KEYS`` (never an account or a
research key); ``/v1/ades/status`` is for admins.
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response

from assay.api.auth import _presented, require_admin

router = APIRouter()


def _worker_keys() -> list[str]:
    return [k.strip() for k in os.environ.get("ASSAY_WORKER_KEYS", "").split(",") if k.strip()]


def require_worker(x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                   authorization: str | None = Header(default=None, alias="Authorization")) -> None:
    got = _presented(x_api_key, authorization) or ""
    if not any(hmac.compare_digest(got, k) for k in _worker_keys()):
        raise HTTPException(401, "worker key required")


def _svc():
    from assay.factordb import service

    try:
        svc = service.get()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if svc.ades is None:
        raise HTTPException(503, "ADES is off on this controller (set ASSAY_ADES=1)")
    return svc


def _name(body: dict) -> str:
    w = str(body.get("worker") or "")
    if not w or len(w) > 80:
        raise HTTPException(422, "worker name required (<= 80 chars)")
    return w


@router.post("/lease", dependencies=[Depends(require_worker)])
def lease(body: dict = Body(...)):
    task = _svc().lease(_name(body), str(body.get("panel") or ""), body.get("info") or {})
    return task if task is not None else Response(status_code=204)


@router.post("/heartbeat", dependencies=[Depends(require_worker)])
def heartbeat(body: dict = Body(...)):
    ok = _svc().ades.heartbeat(str(body.get("task_id")), _name(body))
    if not ok:
        raise HTTPException(409, "lease lost")
    return {"ok": True}


@router.post("/complete", dependencies=[Depends(require_worker)])
def complete(body: dict = Body(...)):
    svc, worker = _svc(), _name(body)
    results = body.get("results") or []
    done = sum(1 for r in results if r.get("metrics") is not None)
    task = svc.ades.complete(str(body.get("task_id")), worker, done, len(results) - done,
                             float(body.get("seconds") or 0))
    if task is None:          # lease expired and the task moved on: drop, never double count
        raise HTTPException(409, "lease lost")
    return svc.apply_results(task, results, float(body.get("seconds") or 0))


@router.post("/fail", dependencies=[Depends(require_worker)])
def fail(body: dict = Body(...)):
    svc = _svc()
    for t in svc.ades.fail(str(body.get("task_id")), _name(body), str(body.get("error") or "")):
        svc._progress(t["job_id"], t["owner"], 0, t["n"], 0.0)
    return {"ok": True}


@router.get("/status", dependencies=[Depends(require_admin)])
def status():
    return _svc().ades.status()
