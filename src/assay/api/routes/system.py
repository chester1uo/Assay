"""System / introspection routes (architecture §4.4).

These are deliberately **best-effort and offline-tolerant**: ``/status``,
``/universes`` and ``/data-calendar`` must answer ``200`` even with no data store,
no credentials and an empty library. But "tolerant" must never mean "dishonest":
a block that *fails* reports an ``error`` string instead of silently degrading to
zeros that look like a healthy-but-empty deployment, and nothing is reported that
the engine does not actually measure.

Endpoints::

    GET /v1/system/status         -> engine / data (per market) / cache / session summary
    GET /v1/system/universes      -> [{id, market, n_symbols, last_rebalance}]
    GET /v1/system/data-calendar  -> [{date, market, n_symbols, coverage_pct}]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from assay import __version__
from assay.api.app import get_service
from assay.api.auth import get_api_key

router = APIRouter()

# Universes the engine knows about (architecture §4.4 / §6.3 enum). Russell2000 has
# no ingested snapshot in any shipped store; it reports 0 symbols and is flagged
# ``wired: false`` rather than pretending to be an available universe.
_KNOWN_UNIVERSES = ("NASDAQ100", "SP500", "Russell2000", "CSI300", "CSI500", "CSI1000")
_UNWIRED = {"Russell2000"}


def _market_dirs(svc) -> dict[str, Path]:
    """Every configured market -> its store root (primary + per-market overrides).

    Defensive: this endpoint must never 5xx, so a config that lacks ``data_dir`` /
    ``market_dirs`` (a stub, or a partially-built service) yields ``{}`` rather than
    raising — callers then report "not ingested" instead of a fake span.
    """
    out: dict[str, Path] = {}
    try:
        market = str(getattr(svc.config, "market", "") or "").upper()
        data_dir = getattr(svc.config, "data_dir", None)
        if market and data_dir is not None:
            out[market] = Path(data_dir)
        for mk, d in (getattr(svc.config, "market_dirs", None) or {}).items():
            out[str(mk).upper()] = Path(d)
    except Exception:  # noqa: BLE001 — never let introspection break /status
        pass
    return out


def _price_span(data_dir: Path, market: str) -> dict[str, Any]:
    """Real ingested span of one market's ``price_raw`` store (or an error)."""
    from assay.data.schemas import price_root

    proot = price_root(Path(data_dir), market)
    files = sorted(proot.glob("**/price_raw.parquet")) if proot.exists() else []
    if not files:
        return {"first_date": None, "last_date": None, "trading_days": 0,
                "symbols_in_store": 0, "last_sync": None, "ingested": False}
    import polars as pl

    df = pl.read_parquet([str(f) for f in files], columns=["date", "symbol", "as_of_date"])
    if not df.height:
        return {"first_date": None, "last_date": None, "trading_days": 0,
                "symbols_in_store": 0, "last_sync": None, "ingested": False}
    return {
        "first_date": str(df["date"].min()),
        "last_date": str(df["date"].max()),
        "trading_days": int(df["date"].n_unique()),
        "symbols_in_store": int(df["symbol"].n_unique()),
        # NB: for EOD bars knowledge-time == trading date, so this equals last_date.
        "last_sync": str(df["as_of_date"].max()),
        "ingested": True,
    }


def _market_block(svc, market: str, data_dir: Path) -> dict[str, Any]:
    """Per-market data facts, measured from that market's own store."""
    block: dict[str, Any] = {"market": market, "data_dir": str(data_dir)}
    try:
        block.update(_price_span(data_dir, market))
    except Exception as exc:  # noqa: BLE001 — surface the failure, never fake zeros
        block.update({"first_date": None, "last_date": None, "trading_days": 0,
                      "symbols_in_store": 0, "last_sync": None, "ingested": False,
                      "error": f"{type(exc).__name__}: {exc}"})
    return block


def _data_summary(svc, primary: dict[str, Any]) -> dict[str, Any]:
    """Primary-market freshness block (the shape the WebUI reads).

    ``symbols_available`` is the *default universe's* size as-of the latest ingested
    date (not the config's default period end, which may predate the data).
    """
    summary: dict[str, Any] = {
        "market": primary.get("market"),
        "last_sync": primary.get("last_sync"),
        "trading_days_available": primary.get("trading_days", 0),
        "symbols_available": 0,
        "first_date": primary.get("first_date"),
        "last_date": primary.get("last_date"),
    }
    if primary.get("error"):
        summary["error"] = primary["error"]
    try:
        universe = svc.config.default_universe
        as_of = summary["last_date"] or svc.config.default_period[1]
        summary["symbols_available"] = len(svc.store.get_universe(universe, as_of, as_of))
    except Exception as exc:  # noqa: BLE001
        summary["symbols_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def _cache_summary(svc) -> dict[str, Any]:
    """Real L2 cache counters.

    Only the L2 factor-result cache exists today (the L1 operator arena is not built —
    see :mod:`assay.cache`), so no L1 numbers are reported rather than hard-coded
    zeros pretending to be a measurement.
    """
    try:
        s = svc.cache.stats()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "l2_entries": int(s.get("entries", 0)),
        "l2_size_gb": float(s.get("bytes", 0)) / 1e9,
        "l2_hits": int(s.get("hits", 0)),
        "l2_misses": int(s.get("misses", 0)),
        "l2_hit_rate": float(s.get("hit_rate", 0.0)),
        "l2_writes": int(s.get("writes", 0)),
        "l2_corrupt": int(s.get("corrupt", 0)),
        "l1": None,  # not implemented (no L1 arena) — explicitly absent, not "0"
    }


def _last_rebalance(data_dir: Path, market: str, universe: str, as_of: str | None) -> str | None:
    """Latest membership ``effective_date`` known for ``universe`` (or None)."""
    try:
        import polars as pl

        from assay.data.schemas import universe_snapshots_path

        p = universe_snapshots_path(Path(data_dir), market)
        if not p.exists():
            return None
        df = pl.read_parquet(str(p), columns=["index_id", "effective_date"]).filter(
            pl.col("index_id") == universe
        )
        if as_of:
            df = df.filter(pl.col("effective_date").cast(pl.Utf8) <= str(as_of))
        return str(df["effective_date"].max()) if df.height else None
    except Exception:  # noqa: BLE001 — a missing/odd store just means "unknown"
        return None


@router.get("/status")
def system_status(api_key: str | None = Depends(get_api_key)) -> dict:
    """Engine version + measured data / cache / session summary (architecture §4.4).

    ``data`` describes the primary market (what the WebUI defaults to); ``markets``
    lists **every configured market** so a multi-market deployment (US + A-share) is
    fully visible instead of only its primary store.
    """
    svc = get_service()
    primary_name = str(getattr(svc.config, "market", "") or "").upper() or None
    markets: list[dict[str, Any]] = []
    try:
        dirs = _market_dirs(svc)
        markets = [_market_block(svc, mk, d) for mk, d in sorted(dirs.items())]
    except Exception:  # noqa: BLE001 — /status must never 5xx
        markets = []
    primary = next((m for m in markets if m.get("market") == primary_name),
                   markets[0] if markets else {"market": primary_name})

    sessions = 0
    try:
        sessions = svc.sessions.active_sessions()
    except Exception:  # noqa: BLE001
        pass

    out: dict[str, Any] = {
        "engine_version": __version__,
        "data": _data_summary(svc, primary),
        "markets": markets,
        "cache": _cache_summary(svc),
        "active_sessions": sessions,
    }
    try:
        out["library_factors"] = len(svc.library_query(limit=-1))
    except Exception as exc:  # noqa: BLE001
        out["library_factors"] = 0
        out["library_error"] = f"{type(exc).__name__}: {exc}"
    return out


@router.get("/universes")
def list_universes(api_key: str | None = Depends(get_api_key)) -> list[dict]:
    """Known universes with symbol counts resolved as-of each market's latest data.

    Counts are taken as-of the market's **latest ingested date** (not the config's
    default period end, which may predate the data), and ``last_rebalance`` is the
    real latest membership ``effective_date``.
    """
    svc = get_service()
    dirs = _market_dirs(svc)
    # latest ingested date per market, measured once
    latest: dict[str, str | None] = {}
    for mk, d in dirs.items():
        try:
            latest[mk] = _price_span(d, mk).get("last_date")
        except Exception:  # noqa: BLE001
            latest[mk] = None

    out: list[dict] = []
    for uid in _KNOWN_UNIVERSES:
        try:
            market = svc._market_for(uid)
        except Exception:  # noqa: BLE001 — never 5xx on introspection
            market = None
        try:
            default_end = svc.config.default_period[1]
        except Exception:  # noqa: BLE001
            default_end = None
        as_of = latest.get(str(market).upper()) or default_end
        row: dict[str, Any] = {"id": uid, "market": market, "n_symbols": 0,
                               "as_of": as_of, "last_rebalance": None,
                               "wired": uid not in _UNWIRED}
        try:
            row["n_symbols"] = len(svc.store_for_universe(uid).get_universe(uid, as_of, as_of))
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
        d = dirs.get(str(market).upper())
        if d is not None:
            row["last_rebalance"] = _last_rebalance(d, market, uid, as_of)
        out.append(row)
    return out


@router.get("/data-calendar")
def data_calendar(
    market: str | None = Query(None, description="Market to report (default: primary)."),
    year: int | None = Query(None, description="Restrict to one calendar year."),
    api_key: str | None = Depends(get_api_key),
) -> list[dict]:
    """Real per-day coverage: symbols present per trading date.

    ``coverage_pct`` is that date's symbol count over the market's busiest date in the
    returned window (1.0 = full coverage). Returns ``[]`` only when nothing is
    ingested for the requested market.
    """
    svc = get_service()
    dirs = _market_dirs(svc)
    mk = str(market or svc.config.market).upper()
    data_dir = dirs.get(mk)
    if data_dir is None:
        return []
    try:
        import polars as pl

        from assay.data.schemas import price_root

        proot = price_root(Path(data_dir), mk)
        if not proot.exists():
            return []
        files = sorted(proot.glob("**/price_raw.parquet"))
        if year is not None:  # prune by the year= partition before reading
            files = [f for f in files if f"year={year}" in str(f)] or files
        if not files:
            return []
        df = pl.read_parquet([str(f) for f in files], columns=["date", "symbol"])
        if year is not None:
            df = df.filter(pl.col("date").dt.year() == int(year))
        if not df.height:
            return []
        per_day = (
            df.group_by("date").agg(pl.col("symbol").n_unique().alias("n_symbols")).sort("date")
        )
        peak = int(per_day["n_symbols"].max()) or 1
        return [
            {"date": str(r["date"]), "market": mk, "n_symbols": int(r["n_symbols"]),
             "coverage_pct": round(int(r["n_symbols"]) / peak, 4)}
            for r in per_day.iter_rows(named=True)
        ]
    except Exception:  # noqa: BLE001 — the calendar is decorative; never break the page
        return []
