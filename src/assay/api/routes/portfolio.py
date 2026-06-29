"""Portfolio backtest route (architecture §4.2; portfolio design-doc Phase 5).

* ``POST /v1/portfolio/backtest`` — run a full portfolio backtest for a factor
  expression and return the section-5 :class:`~assay.portfolio.PortfolioReport`
  as a JSON-safe dict (Sharpe / drawdown / turnover / cost drag / NAV series ...).

Mounts under the ``/v1/portfolio`` prefix (see :mod:`assay.api.app`). The service is
resolved via the lazy :func:`~assay.api.app.get_service` dependency so importing the
app needs no credentials and a missing data store surfaces as HTTP 503 (handled by
the app's exception envelope). The request carries the factor ``expr`` plus the
section-2 :class:`~assay.portfolio.PortfolioBacktestConfig` — supplied either as a
``config`` dict (full :meth:`PortfolioBacktestConfig.from_dict` payload) or as the
required ``period_start`` / ``period_end`` (with any extra config fields), defaulted
to the US/NASDAQ-100 market preset. The ``as_of`` PIT cutoff is forwarded to the run.

Payloads stay NaN-safe: :meth:`PortfolioReport.to_dict` already maps non-finite
floats to ``null``, so the response is valid JSON for the browser's ``JSON.parse``.

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from assay.api.app import get_service
from assay.api.auth import get_api_key
from assay.portfolio import PortfolioBacktestConfig

router = APIRouter()


class PortfolioBacktestRequest(BaseModel):
    """Request body for ``POST /v1/portfolio/backtest`` (portfolio design-doc §2).

    Carries the factor ``expr`` and the section-2 backtest configuration. Provide
    the config one of two ways:

    * ``config`` — a full :meth:`PortfolioBacktestConfig.to_dict` payload (any subset
      of the section-2 fields; unknown keys ignored, missing ones defaulted); or
    * the inline ``period_start`` / ``period_end`` (required by the config validator)
      plus any extra config fields collected from the request's ``extra`` slot.

    When ``config`` is omitted the config is built from the market ``preset`` (US /
    NASDAQ-100 cost & limit defaults) overridden by the inline fields. ``as_of`` is
    the point-in-time knowledge cutoff forwarded to the run (defaults inside the
    backtester to ``config.as_of_date`` then ``period_end``).
    """

    # Allow extra top-level fields so callers can pass config knobs inline
    # (e.g. {"expr": ..., "period_start": ..., "weight_method": "equal"}).
    model_config = ConfigDict(extra="allow")

    expr: str = Field(..., description="Factor expression (qlib or Python syntax).")
    config: dict[str, Any] | None = Field(
        None, description="Full PortfolioBacktestConfig payload (section-2 fields)."
    )
    as_of: str | None = Field(
        None, description="Point-in-time knowledge cutoff (YYYY-MM-DD)."
    )

    # Reserved (non-config) keys that must never leak into the config builder.
    _RESERVED = ("expr", "config", "as_of")

    def build_config(self) -> PortfolioBacktestConfig:
        """Assemble the :class:`PortfolioBacktestConfig` from the request body.

        Precedence: an explicit ``config`` dict is taken verbatim via
        :meth:`PortfolioBacktestConfig.from_dict` (it tolerates partial payloads and
        ignores unknown keys). Otherwise the inline section-2 fields (the extra slot
        plus ``period_start`` / ``period_end`` if present) are merged onto the market
        preset chosen by the inline ``market`` (default US), so a minimal
        ``{expr, period_start, period_end}`` request is valid and an A-share request
        (``market='A'``) gets A-share cost/limit defaults. The config validator
        raises on out-of-range fields (mapped to 422).
        """
        if self.config is not None:
            return PortfolioBacktestConfig.from_dict(self.config)
        # Collect inline config fields from the model's extra slot (anything that is
        # not a reserved transport key), then layer them onto the market preset.
        extra = {
            k: v
            for k, v in (self.model_extra or {}).items()
            if k not in self._RESERVED
        }
        # Pick the cost/limit preset from the requested market (default US), so an
        # inline ``{market: "A"}`` request gets the A-share stamp-duty / commission /
        # price-limit defaults rather than US ones. Unknown markets fall back to US.
        # ``market`` is passed positionally to preset(), so drop it from the kwargs
        # overrides to avoid "multiple values for argument 'market'".
        market = str(extra.pop("market", "US")).upper()
        if market not in ("US", "A", "HK"):
            market = "US"
        return PortfolioBacktestConfig.preset(market, **extra)


@router.post("/backtest")
def backtest_portfolio(
    req: PortfolioBacktestRequest,
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Run a portfolio backtest and return the section-5 report as a JSON-safe dict.

    Builds a :class:`PortfolioBacktestConfig` from the body, then delegates to
    :meth:`AssayService.backtest_portfolio`, which runs the design-doc §1.1 pipeline
    over the service store and returns a :class:`~assay.portfolio.PortfolioReport`.
    A missing data store / credentials surfaces as HTTP 503 via the app's exception
    handler; an invalid config (out-of-range section-2 field) surfaces as 422.
    """
    svc = get_service()
    try:
        # The section-2 validator raises ValueError on an out-of-range / missing field;
        # that is a client error (422), not a 500 — map it into the §4.6 envelope.
        config = req.build_config()
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report = svc.backtest_portfolio(req.expr, config, as_of=req.as_of)
    return report.to_dict()
