"""Universe ingester: NASDAQ-100 membership history -> ``universe_snapshots``.

Emits one snapshot per composition change in range. ``as_of_date`` is set to the
``effective_date`` (an index composition is announced on/before it takes effect,
so the effective date is a conservative knowledge time).
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from assay.config import AssayConfig
from assay.data.io_utils import upsert_parquet
from assay.data.schemas import UNIVERSE_SNAPSHOTS_SCHEMA, universe_snapshots_path
from assay.data.universe import nasdaq100

log = logging.getLogger(__name__)

_BUILDERS = {
    "NASDAQ100": nasdaq100.membership_snapshots,
}


class UniverseIngester:
    def __init__(self, config: AssayConfig):
        self.config = config

    def run(self, index_id: str, start: dt.date, end: dt.date) -> dict:
        builder = _BUILDERS.get(index_id.upper())
        if builder is None:
            raise ValueError(
                f"unknown index_id {index_id!r}; supported: {sorted(_BUILDERS)}"
            )
        snaps = builder(start, end)
        rows = [
            {
                "index_id": index_id.upper(),
                "effective_date": eff,
                "symbols": sorted(members),
                "as_of_date": eff,
            }
            for eff, members in snaps
        ]
        stats = {"snapshots": len(rows), "rows": 0}
        if not rows:
            log.warning("no universe snapshots for %s in %s..%s", index_id, start, end)
            return stats

        df = pl.DataFrame(rows, schema=UNIVERSE_SNAPSHOTS_SCHEMA)
        path = universe_snapshots_path(self.config.data_dir, self.config.market)
        stats["rows"] = upsert_parquet(
            path, df, keys=["index_id", "effective_date"], sort_by=["index_id", "effective_date"]
        )
        log.info("wrote %s (%d snapshots this run, %d total rows)", path, len(rows), stats["rows"])
        return stats
