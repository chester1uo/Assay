"""Factor-library routes (architecture §4.3).

The library is the credential-free surface: listing, fetching, saving and deleting
reports never touch the data store. Re-evaluation paths (``/correlation-matrix``,
``/prune`` when computing similarity) *do* load a panel via the service, so those may
surface HTTP 503 when no data is ingested.

Endpoints::

    GET    /v1/library/factors               -> {total, factors}
    GET    /v1/library/factors/{factor_id}   -> FactorReport (full)
    POST   /v1/library/factors               -> {factor_id, saved}
    DELETE /v1/library/factors               -> {deleted}
    GET    /v1/library/correlation-matrix    -> {factor_ids, matrix}
    POST   /v1/library/prune                 -> {would_delete, count, deleted, dry_run}
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from assay.api.app import get_service
from assay.api.auth import get_api_key
from assay.api.models import (
    DeleteFactorsRequest,
    DeleteFactorsResponse,
    LibraryListResponse,
    PruneRequest,
    PruneResponse,
    SaveFactorResponse,
)
from assay.library import FactorReport

router = APIRouter()


def _split_ids(factor_ids: str | None) -> list[str]:
    """Parse a comma-separated ``factor_ids`` query param into a clean id list."""
    if not factor_ids:
        return []
    return [f.strip() for f in factor_ids.split(",") if f.strip()]


def _parse_period(period: str | None) -> tuple[str, str] | None:
    """Parse a ``start,end`` query param into a (start, end) tuple, or ``None``."""
    if not period:
        return None
    parts = [p.strip() for p in period.split(",") if p.strip()]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


@router.get("/factors", response_model=LibraryListResponse)
async def list_factors(
    universe: str | None = Query(None),
    min_rank_icir: float = Query(0.0),
    max_redundancy: float = Query(1.0),
    source: str | None = Query(None),
    sort_by: str = Query("rank_icir"),
    limit: int = Query(100),
    offset: int = Query(0),
    api_key: str | None = Depends(get_api_key),
) -> LibraryListResponse:
    """Filtered / sorted / paged library view -> ``{total, factors}`` (architecture §4.3)."""
    svc = get_service()
    summaries = svc.library_query(
        universe=universe,
        min_rank_icir=min_rank_icir,
        max_redundancy=max_redundancy,
        source=source,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )
    return LibraryListResponse(
        total=len(summaries),
        factors=[s.to_dict() for s in summaries],
    )


@router.get("/factors/{factor_id}")
async def get_factor(
    factor_id: str,
    api_key: str | None = Depends(get_api_key),
) -> dict:
    """Full :class:`FactorReport` for ``factor_id`` (404 when unknown)."""
    svc = get_service()
    report = svc.library.get(factor_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"factor {factor_id!r} not found")
    return report.to_dict()


@router.post("/factors", response_model=SaveFactorResponse)
async def save_factor(
    report: dict,
    api_key: str | None = Depends(get_api_key),
) -> SaveFactorResponse:
    """Persist a posted :class:`FactorReport` body -> ``{factor_id, saved}``.

    The body is the full report dict (as returned by ``/evaluate``); it is rebuilt via
    :meth:`FactorReport.from_dict` and appended to the library.
    """
    svc = get_service()
    try:
        fr = FactorReport.from_dict(report)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid FactorReport body: {exc}") from exc
    fid = svc.library.save(fr)
    return SaveFactorResponse(factor_id=fid, saved=True)


@router.post("/factors/bulk")
def bulk_add_factors(
    req: dict,
    api_key: str | None = Depends(get_api_key),
) -> dict:
    """Evaluate a batch of expressions and save the good ones (bulk import).

    Body: ``{exprs:[str], universe?, source?, period?:[start,end], as_of?}``. Returns
    ``{evaluated, saved, results:[{expr, factor_id, saved, failure_mode, rank_ic,
    rank_icir}]}``. The WebUI chunks a large import into several calls to drive a
    progress bar. A missing data store surfaces as HTTP 503 via the app handler.
    """
    svc = get_service()
    exprs = [str(e).strip() for e in (req.get("exprs") or []) if str(e).strip()]
    if not exprs:
        return {"evaluated": 0, "saved": 0, "results": []}
    period = req.get("period")
    if isinstance(period, (list, tuple)) and len(period) == 2:
        period = (period[0], period[1])
    else:
        period = None
    return svc.add_factors(
        exprs,
        universe=req.get("universe"),
        source=(req.get("source") or "CUSTOM"),
        period=period,
        as_of=req.get("as_of"),
    )


@router.delete("/factors", response_model=DeleteFactorsResponse)
async def delete_factors(
    req: DeleteFactorsRequest,
    api_key: str | None = Depends(get_api_key),
) -> DeleteFactorsResponse:
    """Delete the given factor ids -> ``{deleted}`` (architecture §4.3)."""
    svc = get_service()
    n = svc.library.delete(req.factor_ids)
    return DeleteFactorsResponse(deleted=n)


@router.get("/correlation-matrix")
def correlation_matrix(
    factor_ids: str | None = Query(None, description="Comma-separated factor ids."),
    universe: str | None = Query(None),
    period: str | None = Query(None, description="start,end dates."),
    as_of: str | None = Query(None),
    adj: str | None = Query(None),
    api_key: str | None = Depends(get_api_key),
) -> dict:
    """Signed-Spearman similarity matrix over stored factors -> ``{factor_ids, matrix}``."""
    svc = get_service()
    ids = _split_ids(factor_ids)
    return svc.correlation_matrix(
        ids,
        universe=universe,
        period=_parse_period(period),
        as_of=as_of,
        adj=adj,
    )


@router.post("/prune", response_model=PruneResponse)
def prune_factors(
    req: PruneRequest,
    api_key: str | None = Depends(get_api_key),
) -> PruneResponse:
    """Identify (and optionally delete) redundant factors -> ``{would_delete, count, ...}``.

    Re-evaluates the candidate factors on one shared engine, builds the similarity
    matrix and runs the greedy :func:`assay.library.prune`. With ``dry_run=true``
    nothing is deleted; otherwise the would-delete set is removed from the library.
    """
    svc = get_service()
    from assay.library import prune as _prune

    # Candidate ids: explicit list, else every stored factor.
    if req.factor_ids:
        ids = req.factor_ids
    else:
        ids = [s.factor_id for s in svc.library_query(limit=-1)]

    if not ids:
        return PruneResponse(would_delete=[], count=0, deleted=0, dry_run=req.dry_run)

    corr = svc.correlation_matrix(ids, universe=req.universe, period=_parse_period(req.period))
    matrix = corr["matrix"]
    used_ids = corr["factor_ids"]
    # Quality scores (rank_icir) for tie-breaking, from the library index.
    scores: dict[str, float] = {}
    for s in svc.library_query(limit=-1):
        v = s.rank_icir
        scores[s.factor_id] = float(v) if v is not None else float("-inf")

    result = _prune(matrix, used_ids, scores, threshold=req.redundancy_threshold)
    would_delete = result["would_delete"]

    deleted = 0
    if not req.dry_run and would_delete:
        deleted = svc.library.delete(would_delete)

    return PruneResponse(
        would_delete=would_delete,
        count=len(would_delete),
        deleted=deleted,
        dry_run=req.dry_run,
    )
