"""Factor-combination route (design-doc §6.3).

* ``POST /v1/combination`` — combine several factors (selected from the library /
  Alpha catalogs or supplied inline) into one composite alpha, fit the combination
  weights on a **train** window, optionally select the scheme on a **validation**
  window, and report the composite's IC/RankIC/ICIR on **train / val / test**.

Mounts under ``/v1/combination`` (see :mod:`assay.api.app`). The service is resolved
via the lazy :func:`~assay.api.app.get_service` dependency, so a missing data store
surfaces as the structured ``{"failure": "NO_DATA"}`` payload the
:meth:`AssayService.combine_factors` returns (HTTP 200 — the UI renders it), while an
invalid knob (bad ``method`` / out-of-range field) maps to HTTP 422.

Payloads are JSON-safe: :meth:`CombinationResult.to_dict` already maps non-finite
floats to ``null``.

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from assay.api.app import get_service
from assay.api.auth import get_api_key

router = APIRouter()


class CombineRequest(BaseModel):
    """Request body for ``POST /v1/combination`` (design-doc §6.3).

    ``factors`` selects the constituents: each item is a bare expression string, a
    pool reference (``"lib:<id>"``, ``"alpha101:<n>"``, ``"alpha158:<n>"``), or a
    dict ``{"name": ..., "expr": ...}`` / ``{"name": ..., "id": ...}``. ``train`` /
    ``val`` / ``test`` are ``[start, end]`` ``YYYY-MM-DD`` windows. The remaining
    fields mirror :meth:`AssayService.combine_factors` (universe/horizons/execution/
    as_of/adj resolve from config when omitted; combination knobs default sensibly).
    """

    factors: list[Any] = Field(..., description="Factor specs (expr / lib:id / alphaNNN:n / {name,expr|id}).")
    train: list[str] = Field(..., min_length=2, max_length=2, description="[start, end].")
    val: list[str] = Field(..., min_length=2, max_length=2, description="[start, end].")
    test: list[str] = Field(..., min_length=2, max_length=2, description="[start, end].")
    universe: str | None = None
    horizons: list[int] | None = None
    execution: str | None = None
    as_of: str | None = None
    adj: str | None = None
    method: str = Field("icir_weight", description="A scheme name or 'auto' (validation-selected).")
    standardize: str = Field("zscore", description="'zscore' | 'rank'.")
    horizon: int | None = Field(None, description="Headline horizon (defaults to the smallest).")
    ridge_lambda: float = 10.0
    embargo: int | None = Field(None, description="Label purge; defaults to max horizon.")
    candidate_methods: list[str] | None = None
    model_params: dict[str, Any] | None = Field(None, description="Hyper-params for the learned model.")


@router.get("/methods")
def list_methods(api_key: str | None = Depends(get_api_key)) -> dict[str, Any]:
    """List the available combination methods -> ``{methods: [{name, kind, available}]}``.

    Credential-free (no data load). Learned models are flagged ``available=False``
    when their library (scikit-learn / lightgbm / xgboost) is not installed.
    """
    return {"methods": get_service().combination_methods()}


# Registered for both ``/v1/combination`` and ``/v1/combination/`` so a trailing
# slash (proxies, manual curls) does not fall through to the static handler (405).
@router.post("")
@router.post("/")
def combine_factors(
    req: CombineRequest,
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Combine factors and score the composite out-of-sample -> JSON-safe dict.

    Delegates to :meth:`AssayService.combine_factors`. A missing data store / no
    resolvable factor returns the structured ``{"failure": ...}`` payload (HTTP 200);
    an invalid knob (e.g. an unknown ``method`` / ``standardize``) maps to HTTP 422.
    """
    svc = get_service()
    if not req.factors:
        raise HTTPException(status_code=422, detail="at least one factor is required")
    try:
        return svc.combine_factors(
            req.factors,
            train=(req.train[0], req.train[1]),
            val=(req.val[0], req.val[1]),
            test=(req.test[0], req.test[1]),
            universe=req.universe,
            horizons=req.horizons,
            execution=req.execution,
            as_of=req.as_of,
            adj=req.adj,
            method=req.method,
            standardize=req.standardize,
            horizon=req.horizon,
            ridge_lambda=req.ridge_lambda,
            embargo=req.embargo,
            candidate_methods=req.candidate_methods,
            model_params=req.model_params,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
