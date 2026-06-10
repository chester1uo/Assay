"""Pydantic v2 request/response models for the REST API (architecture §4).

These are the *wire* schema for the FastAPI app — thin, lenient envelopes over the
:class:`~assay.service.AssayService` keyword surface (architecture §4.2-§4.4). Every
evaluation parameter is optional: the service resolves missing fields from the
config defaults (``default_universe`` / ``default_period`` / ...), so an agent can
``POST {"expr": "..."}`` and get a full report. Responses are mostly assembled as
plain dicts from ``FactorReport.to_dict()`` / ``FactorSummary.to_dict()`` (those are
already JSON-safe), so the response models here document the shape without forcing a
re-validation round-trip that could reject a valid report.

House style: ``from __future__ import annotations``, type hints everywhere, concise
field docstrings via :class:`~pydantic.Field` descriptions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EvaluateRequest",
    "BatchRequest",
    "SessionCreateRequest",
    "SaveFactorResponse",
    "DeleteFactorsRequest",
    "DeleteFactorsResponse",
    "PruneRequest",
    "PruneResponse",
    "BatchResponse",
    "LibraryListResponse",
    "ErrorBody",
    "ErrorResponse",
]


# A 2-tuple [start, end] date window; kept as a free list so callers may send
# either ["2020-01-01", "2024-12-31"] or omit it entirely (service fills default).
Period = list[str]


class EvaluateRequest(BaseModel):
    """Body for ``POST /v1/factor/evaluate`` (architecture §4.2).

    Only ``expr`` is required. ``stream`` toggles the SSE response; ``session_id``
    binds the call to a pre-loaded panel (header ``X-Session-Id`` is also accepted by
    the route and overrides this field when present). All other fields are lenient
    optionals resolved to config defaults by the service.
    """

    model_config = ConfigDict(extra="ignore")

    expr: str = Field(..., description="Factor expression (qlib or Python syntax).")
    universe: str | None = Field(None, description="Universe / index id, e.g. NASDAQ100.")
    period: Period | None = Field(None, description="[start, end] dates, inclusive.")
    horizons: list[int] | None = Field(None, description="Forward-return horizons (days).")
    execution: str | None = Field(None, description="Fill model: next_open | next_close | close.")
    neutralize: list[str] | None = Field(None, description="Cross-section neutralisers, e.g. ['sector'].")
    as_of: str | None = Field(None, description="Point-in-time knowledge cutoff (YYYY-MM-DD).")
    adj: str | None = Field(None, description="Price adjustment: none | split | total.")
    stream: bool = Field(False, description="Stream SSE events instead of a single JSON report.")
    session_id: str | None = Field(None, description="Pre-loaded session id (skips panel load).")
    save: bool = Field(False, description="Persist the resulting report to the library.")

    def service_kwargs(self) -> dict[str, Any]:
        """Keyword dict for :meth:`AssayService.evaluate` (drops transport-only fields).

        ``stream`` is consumed by the route, not the service; ``period`` is passed as
        a tuple. ``None`` values are dropped so the service applies its own defaults.
        """
        kw: dict[str, Any] = {
            "universe": self.universe,
            "period": tuple(self.period) if self.period else None,
            "horizons": self.horizons,
            "execution": self.execution,
            "neutralize": self.neutralize,
            "as_of": self.as_of,
            "adj": self.adj,
            "session_id": self.session_id,
            "save": self.save,
        }
        return {k: v for k, v in kw.items() if v is not None}


class BatchRequest(BaseModel):
    """Body for ``POST /v1/factor/batch`` (architecture §4.2)."""

    model_config = ConfigDict(extra="ignore")

    exprs: list[str] = Field(..., description="Factor expressions to evaluate in parallel.")
    universe: str | None = Field(None, description="Shared universe for the batch.")
    period: Period | None = Field(None, description="Shared [start, end] window.")
    horizons: list[int] | None = Field(None, description="Forward-return horizons (days).")
    execution: str | None = Field(None, description="Fill model.")
    neutralize: list[str] | None = Field(None, description="Cross-section neutralisers.")
    as_of: str | None = Field(None, description="Point-in-time cutoff.")
    adj: str | None = Field(None, description="Price adjustment mode.")
    n_jobs: int | None = Field(None, description="Worker threads (defaults to config.n_workers).")
    sort_by: str = Field("rank_icir", description="Report attribute to sort by, descending.")
    session_id: str | None = Field(None, description="Reuse an existing session.")
    save: bool = Field(False, description="Persist every resulting report.")

    def service_kwargs(self) -> dict[str, Any]:
        """Keyword dict for :meth:`AssayService.batch` (``exprs`` passed positionally)."""
        kw: dict[str, Any] = {
            "universe": self.universe,
            "period": tuple(self.period) if self.period else None,
            "horizons": self.horizons,
            "execution": self.execution,
            "neutralize": self.neutralize,
            "as_of": self.as_of,
            "adj": self.adj,
            "n_jobs": self.n_jobs,
            "sort_by": self.sort_by,
            "session_id": self.session_id,
            "save": self.save,
        }
        return {k: v for k, v in kw.items() if v is not None}


class SessionCreateRequest(BaseModel):
    """Body for ``POST /v1/session/create`` (architecture §4.2)."""

    model_config = ConfigDict(extra="ignore")

    universe: str | None = Field(None, description="Universe to pre-load.")
    period: Period | None = Field(None, description="[start, end] window to pre-load.")
    as_of: str | None = Field(None, description="Point-in-time cutoff for the panel.")
    adj: str | None = Field(None, description="Price adjustment mode.")

    def service_kwargs(self) -> dict[str, Any]:
        """Keyword dict for :meth:`AssayService.create_session`."""
        kw: dict[str, Any] = {
            "universe": self.universe,
            "period": tuple(self.period) if self.period else None,
            "as_of": self.as_of,
            "adj": self.adj,
        }
        return {k: v for k, v in kw.items() if v is not None}


# --- library bodies --------------------------------------------------------
class DeleteFactorsRequest(BaseModel):
    """Body for ``DELETE /v1/library/factors`` (architecture §4.3)."""

    model_config = ConfigDict(extra="ignore")

    factor_ids: list[str] = Field(..., description="Factor ids to delete from the library.")


class PruneRequest(BaseModel):
    """Body for ``POST /v1/library/prune`` (architecture §4.3)."""

    model_config = ConfigDict(extra="ignore")

    redundancy_threshold: float = Field(0.7, description="|similarity| at/above which to prune.")
    dry_run: bool = Field(True, description="If true, report would-delete ids without deleting.")
    universe: str | None = Field(None, description="Universe to re-evaluate factors on.")
    period: Period | None = Field(None, description="[start, end] window for re-evaluation.")
    factor_ids: list[str] | None = Field(None, description="Restrict pruning to these ids (else all).")


# --- response envelopes ----------------------------------------------------
# Reports/summaries are passed through as dicts (already JSON-safe from to_dict),
# so response models use loose dict types rather than re-validating the schema.
class BatchResponse(BaseModel):
    """Response for ``POST /v1/factor/batch``."""

    total: int
    elapsed_ms: float
    reports: list[dict[str, Any]]


class LibraryListResponse(BaseModel):
    """Response for ``GET /v1/library/factors``."""

    total: int
    factors: list[dict[str, Any]]


class SaveFactorResponse(BaseModel):
    """Response for ``POST /v1/library/factors``."""

    factor_id: str
    saved: bool = True


class DeleteFactorsResponse(BaseModel):
    """Response for ``DELETE /v1/library/factors``."""

    deleted: int


class PruneResponse(BaseModel):
    """Response for ``POST /v1/library/prune``."""

    would_delete: list[str]
    count: int
    deleted: int = 0
    dry_run: bool = True


# --- error schema (architecture §4.6) --------------------------------------
class ErrorBody(BaseModel):
    """One problem in the §4.6 error envelope (mirrors a diagnostics ``Diagnostic``)."""

    code: str
    name: str
    failure_mode: str | None = None
    severity: str = "error"
    stage: str | None = None
    message: str
    location: dict[str, Any] | None = None
    suggestion: str | None = None
    factor_id: str | None = None


class ErrorResponse(BaseModel):
    """Top-level ``{"error": {...}}`` envelope returned on every non-2xx (architecture §4.6)."""

    error: ErrorBody
