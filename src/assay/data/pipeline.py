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
from assay.data.universe import nasdaq100

log = logging.getLogger(__name__)


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
    """Prepare the full NASDAQ-100 dataset for ``[start, end]``.

    The download universe is the union of every ticker that was a member at any
    point in the range, so survivorship bias is avoided (de-listed/removed names
    are still fetched).
    """
    tickers = sorted(nasdaq100.union_over_range(start, end))
    report: dict = {
        "index": index_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_tickers": len(tickers),
    }
    log.info("preparing %s: %d distinct tickers over %s..%s", index_id, len(tickers), start, end)

    if do_universe:
        report["universe"] = UniverseIngester(config).run(index_id, start, end)
    if do_corp_actions:
        # Corporate actions are *best-effort* adjustment data: a missing/malformed
        # local corp-actions tree must not abort the run and lose the essential
        # price transfer. On failure we record it and continue; prices remain
        # usable unadjusted (adj="none"), and corp-actions can be backfilled later.
        try:
            report["corp_actions"] = CorpActionIngester(config).run(tickers, start, end)
        except Exception as exc:  # noqa: BLE001 - resilience boundary, error is recorded
            log.warning(
                "corporate-actions ingest failed (%s: %s); continuing without adj_events. "
                "Re-run `corp-actions` later to backfill adjustments.",
                type(exc).__name__, exc,
            )
            report["corp_actions"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if do_prices:
        report["prices"] = PriceIngester(config).run(start, end, symbols=set(tickers))
    return report
