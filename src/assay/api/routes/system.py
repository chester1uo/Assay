"""System / introspection routes (architecture §4.4).

These are deliberately **best-effort and offline-tolerant**: ``/status``,
``/universes`` and ``/data-calendar`` must answer ``200`` even with no data store,
no credentials and an empty library — every data access is guarded and degrades to a
null/empty value rather than raising. (The data store is built lazily; touching it
without ingested data raises, so we catch those and report the cache/session/library
facts we *can* see.)

Endpoints::

    GET /v1/system/status         -> engine/data/cache/session summary
    GET /v1/system/universes      -> [{id, n_symbols, last_rebalance}]
    GET /v1/system/data-calendar  -> [{date, coverage_pct, last_sync}]  ([] if no data)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from assay import __version__
from assay.api.app import get_service
from assay.api.auth import get_api_key

router = APIRouter()

# Universes the engine knows about (architecture §4.4 / §6.3 enum). US (NASDAQ100)
# and A-share (CSI300/500/1000) are wired; each reports symbols from its own market
# store. Universes with no ingested snapshot report 0 symbols rather than failing.
_KNOWN_UNIVERSES = ("NASDAQ100", "SP500", "Russell2000", "CSI300", "CSI500", "CSI1000")


def _data_summary(svc) -> dict[str, Any]:
    """Best-effort data-freshness block; tolerates a missing/empty store.

    Reports the actual ingested span (``first_date``/``last_date``/distinct
    ``trading_days_available``) by scanning the ``price_raw`` partitions — so the
    WebUI can default its evaluation period to data that exists instead of an
    empty range. Every read is guarded; a data-less deployment keeps the zeros.
    """
    summary: dict[str, Any] = {
        "market": svc.config.market,
        "last_sync": None,
        "trading_days_available": 0,
        "symbols_available": 0,
        "first_date": None,
        "last_date": None,
    }
    # Resolve the ingested span first so the universe count can be taken as-of the
    # latest data date (the config's default period may predate the actual data).
    try:
        import polars as pl

        from assay.data.schemas import price_root

        proot = price_root(svc.config.data_dir, svc.config.market)
        files = sorted(proot.glob("**/price_raw.parquet")) if proot.exists() else []
        if files:
            df = pl.read_parquet([str(f) for f in files], columns=["date", "as_of_date"])
            if df.height:
                summary["trading_days_available"] = int(df["date"].n_unique())
                summary["first_date"] = str(df["date"].min())
                summary["last_date"] = str(df["date"].max())
                summary["last_sync"] = str(df["as_of_date"].max())
    except Exception:
        pass
    try:
        universe = svc.config.default_universe
        # As-of the latest ingested date when known, else the config default end.
        as_of = summary["last_date"] or svc.config.default_period[1]
        symbols = svc.store.get_universe(universe, as_of, as_of)
        summary["symbols_available"] = len(symbols)
    except Exception:
        pass
    return summary


def _cache_summary(svc) -> dict[str, Any]:
    """L2 cache counters (architecture §4.4 cache block). L1 fields are placeholders."""
    block: dict[str, Any] = {
        "l1_entries": 0,
        "l1_hit_rate_1h": None,
        "l2_entries": 0,
        "l2_size_gb": 0.0,
    }
    try:
        stats = svc.cache.stats()
        block["l2_entries"] = int(stats.get("entries", 0))
        block["l2_size_gb"] = float(stats.get("bytes", 0)) / 1e9
    except Exception:
        pass
    return block


@router.get("/status")
def system_status(api_key: str | None = Depends(get_api_key)) -> dict:
    """Engine version + best-effort data / cache / session summary (architecture §4.4).

    Always ``200``: every data-touching read is guarded so a credential-less, data-
    less deployment still reports its engine version, cache footprint and active
    sessions.
    """
    svc = get_service()
    library_count = 0
    try:
        library_count = len(svc.library_query(limit=-1))
    except Exception:
        pass
    return {
        "engine_version": __version__,
        "data": _data_summary(svc),
        "cache": _cache_summary(svc),
        "active_sessions": svc.sessions.active_sessions(),
        "library_factors": library_count,
    }


@router.get("/universes")
def list_universes(api_key: str | None = Depends(get_api_key)) -> list[dict]:
    """Known universes with current symbol counts (architecture §4.4).

    Symbol counts are resolved point-in-time as of the config's default period end;
    universes with no ingested snapshot report ``n_symbols = 0`` rather than failing.
    """
    svc = get_service()
    _start, end = svc.config.default_period
    out: list[dict] = []
    for uid in _KNOWN_UNIVERSES:
        n = 0
        market = svc._market_for(uid)
        try:
            # Count from the universe's own market store (US vs A-share).
            n = len(svc.store_for_universe(uid).get_universe(uid, end, end))
        except Exception:
            pass
        out.append({"id": uid, "n_symbols": n, "market": market, "last_rebalance": None})
    return out


@router.get("/data-calendar")
def data_calendar(
    market: str | None = Query(None),
    year: int | None = Query(None),
    api_key: str | None = Depends(get_api_key),
) -> list[dict]:
    """Per-day coverage calendar; ``[]`` when no data is ingested (architecture §4.4).

    A full trading-day coverage index is a planned data-layer feature; until it
    exists this returns an empty list (never an error) so the WebUI calendar renders
    blank rather than breaking.
    """
    return []
