"""High-level orchestration for preparing a backtest-ready dataset.

Phase 1 target: NASDAQ-100. :func:`prepare_nasdaq100` resolves the point-in-time
constituent universe over a date range, then runs the three ingesters
(universe -> corporate actions -> prices) into the parquet stores. Every stage
reads from the local MASSIVE mirror (``config.massive.source_dir``); nothing is
downloaded.
"""

from __future__ import annotations

import datetime as dt
import logging

from assay.config import AssayConfig
from assay.data.ingest import CorpActionIngester, PriceIngester, UniverseIngester
from assay.data.universe import nasdaq100, sp500

log = logging.getLogger(__name__)

# US index -> survivorship-free union-over-range function. Prices come from the same
# MASSIVE all-US-stocks mirror, so adding an index is just its membership history.
_US_UNION = {
    "NASDAQ100": nasdaq100.union_over_range,
    "SP500": sp500.union_over_range,
}


def prepare_us(
    config: AssayConfig,
    start: dt.date,
    end: dt.date,
    *,
    index_ids: "tuple[str, ...] | list[str]" = ("NASDAQ100",),
    do_universe: bool = True,
    do_corp_actions: bool = True,
    do_prices: bool = True,
) -> dict:
    """Prepare one or more US indices over ``[start, end]`` from the MASSIVE mirror.

    Resolves the survivorship-free constituent union across every requested index,
    writes a ``universe_snapshots`` block per index, then ingests corporate actions
    and prices ONCE for the combined ticker set (prices are the same all-US-stocks
    mirror, so multiple indices share the panel). Nothing is downloaded here.
    """
    ids = [i.upper() for i in index_ids]
    tickers: set[str] = set()
    for iid in ids:
        union = _US_UNION.get(iid)
        if union is None:
            raise ValueError(f"unknown US index {iid!r}; supported: {sorted(_US_UNION)}")
        tickers |= set(union(start, end))
    tickers_sorted = sorted(tickers)
    report: dict = {
        "indices": ids,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_tickers": len(tickers_sorted),
    }
    log.info("preparing US %s: %d distinct tickers over %s..%s", ids, len(tickers_sorted), start, end)

    if do_universe:
        report["universe"] = {iid: UniverseIngester(config).run(iid, start, end) for iid in ids}
    if do_corp_actions:
        # Best-effort: a missing/malformed corp-actions tree must not abort the price
        # transfer. Prices stay usable unadjusted; backfill adjustments later.
        try:
            report["corp_actions"] = CorpActionIngester(config).run(tickers_sorted, start, end)
        except Exception as exc:  # noqa: BLE001 - resilience boundary
            log.warning("corporate-actions ingest failed (%s: %s); continuing without adj_events.",
                        type(exc).__name__, exc)
            report["corp_actions"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if do_prices:
        report["prices"] = PriceIngester(config).run(start, end, symbols=set(tickers_sorted))
    return report


def prepare_nasdaq100(
    config: AssayConfig,
    start: dt.date,
    end: dt.date,
    *,
    do_universe: bool = True,
    do_corp_actions: bool = True,
    do_prices: bool = True,
    index_id: str = "NASDAQ100",
) -> dict:
    """Prepare a single US index (default NASDAQ-100) — thin wrapper over :func:`prepare_us`.

    Kept for backward compatibility; the survivorship-free union avoids survivorship
    bias (removed/de-listed names are still fetched). Use :func:`prepare_us` directly
    to prepare several indices (e.g. NASDAQ100 + SP500) in one pass.
    """
    return prepare_us(
        config, start, end, index_ids=(index_id,),
        do_universe=do_universe, do_corp_actions=do_corp_actions, do_prices=do_prices,
    )
