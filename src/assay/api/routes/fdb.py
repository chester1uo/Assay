"""``/v1/fdb/*``: FactorDB for agents and scripts (see ``docs/en/design/factordb.md``).

Same operations as the ``fdb_*`` MCP tools. The caller (account or key owner) is the
owner of submitted factors and jobs, and is charged the compute time.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from assay.api.auth import get_api_key

router = APIRouter()


def _svc():
    from assay.factordb import service

    try:
        return service.get()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _owner(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.username if user is not None else "anonymous"


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/markets")
def markets(_k=Depends(get_api_key)):
    return _svc().markets()


@router.post("/submit")
def submit(request: Request, body: dict = Body(...), _k=Depends(get_api_key)):
    """Score many expressions on one spec. Known factors answer at once; the rest become a job."""
    exprs = body.get("exprs") or []
    if not isinstance(exprs, list) or not exprs or not all(isinstance(e, str) for e in exprs):
        raise HTTPException(422, "exprs must be a non-empty list of expressions")
    if len(exprs) > 5000:
        raise HTTPException(413, "at most 5000 expressions per submit")
    svc, owner = _svc(), _owner(request)
    if svc.over_budget(owner):
        raise HTTPException(429, "daily compute budget used up; already-scored factors stay queryable")
    if not body.get("universe") or not body.get("start"):
        raise HTTPException(422, "universe and start are required")
    return _call(svc.submit, owner, exprs, universe=body["universe"], start=body["start"], end=body.get("end"),
                      horizons=body.get("horizons") or (1, 5, 10, 20), execution=body.get("execution", "next_open"),
                      tags=body.get("tags") or (), parents=body.get("parents") or (), label=str(body.get("label", "")))


@router.get("/jobs/{job_id}")
def job(job_id: str, top: int = Query(10, ge=0, le=100), _k=Depends(get_api_key)):
    return _svc().job(job_id, top=top)


@router.get("/jobs")
def jobs(request: Request, limit: int = Query(20, ge=1, le=200), _k=Depends(get_api_key)):
    return {"jobs": _svc().reg.jobs(_owner(request), limit)}


@router.get("/factors")
def factors(spec: str | None = None, sort_by: str = "rank_icir", descending: bool = True,
            min_abs_rank_icir: float | None = None, owner: str | None = None, tag: str | None = None,
            limit: int = Query(20, ge=1, le=500), offset: int = Query(0, ge=0), _k=Depends(get_api_key)):
    try:
        return _svc().query(spec=spec, sort_by=sort_by, descending=descending, min_abs_rank_icir=min_abs_rank_icir,
                            owner=owner, tag=tag, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/factors/{factor_id}")
def factor(factor_id: str, _k=Depends(get_api_key)):
    return _svc().factor(factor_id)


@router.post("/novelty")
def novelty(body: dict = Body(...), _k=Depends(get_api_key)):
    if not body.get("expr") or not body.get("universe") or not body.get("start"):
        raise HTTPException(422, "expr, universe and start are required")
    return _call(_svc().novelty, body["expr"], universe=body["universe"], start=body["start"], end=body.get("end"),
                          horizons=body.get("horizons") or (1, 5, 10, 20), execution=body.get("execution", "next_open"),
                          top=int(body.get("top", 5)))


@router.get("/values")
def values(expr: str, universe: str, date: str | None = None, top: int = Query(10, ge=1, le=100),
           _k=Depends(get_api_key)):
    return _call(_svc().values, expr, universe=universe, date=date, top=top)


@router.get("/operators")
def ops(_k=Depends(get_api_key)):
    from assay.factordb.agent import FactorService

    return FactorService.operators()


@router.get("/budget")
def budget(request: Request, _k=Depends(get_api_key)):
    return _svc().budget(_owner(request))
