"""Market-data routes (TradingView-style chart source).

* ``GET  /v1/market/bars``          — OHLCV bars for one symbol (US or A-share),
  daily / weekly / monthly (intraday returns ``available=false`` — only daily
  aggregates are ingested), with ``adj`` = ``none`` | ``split`` | ``total``.
* ``POST /v1/market/factor-series`` — evaluate an alpha expression for one symbol
  and return its daily value series, so any time-series factor can be overlaid on
  the price chart.

Both delegate to :class:`AssayService`, which infers the market from the symbol and
routes to the right per-market store. Payloads are JSON-safe (NaN/inf -> null).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from assay.api.app import get_service
from assay.api.auth import get_api_key

router = APIRouter()


@router.get("/bars")
def market_bars(
    symbol: str = Query(..., description="Ticker — US (e.g. AAPL) or A-share (e.g. 000001.SZ)."),
    freq: str = Query("1d", description="1min|5min|15min|1d|1w|1mo (intraday is unavailable)."),
    adj: str = Query("none", description="none|split|total (alias forward)."),
    start: str | None = Query(None, description="YYYY-MM-DD (defaults to config period start)."),
    end: str | None = Query(None, description="YYYY-MM-DD (defaults to config period end)."),
    as_of: str | None = Query(None, description="Point-in-time cutoff (defaults to end)."),
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Return ``{symbol, market, freq, adj, available, bars:[{date,open,high,low,close,volume}]}``."""
    svc = get_service()
    period = (start, end) if (start and end) else None
    return svc.get_bars(symbol, period=period, freq=freq, adj=adj, as_of=as_of)


@router.post("/factor-series")
def market_factor_series(
    symbol: str = Body(..., embed=True),
    expr: str = Body(..., embed=True),
    start: str | None = Body(None, embed=True),
    end: str | None = Body(None, embed=True),
    adj: str = Body("none", embed=True),
    as_of: str | None = Body(None, embed=True),
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Return ``{symbol, market, expr, dates:[iso], values:[float|null]}`` for one symbol."""
    svc = get_service()
    period = (start, end) if (start and end) else None
    return svc.factor_series(symbol, expr, period=period, adj=adj, as_of=as_of)
