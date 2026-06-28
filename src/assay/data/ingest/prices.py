"""Price ingester: local MASSIVE day-aggregate parquet -> ``price_raw`` parquet.

Reads locally-downloaded day-aggregate files over a date range, optionally
filtered to a symbol universe, normalizes them to the ``price_raw`` schema, and
writes month partitions (``market=US/year=YYYY/month=MM/price_raw.parquet``).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import polars as pl

from assay.config import AssayConfig
from assay.data.io_utils import upsert_parquet
from assay.data.massive.flatfiles import DayAggFile, LocalFlatFiles
from assay.data.schemas import PRICE_RAW_SCHEMA, price_partition_path

log = logging.getLogger(__name__)


def normalize_day_agg(df: pl.DataFrame, source_id: str) -> pl.DataFrame:
    """Map a raw day-aggregate frame to the ``price_raw`` schema.

    ``as_of_date`` is set to the trading ``date``: an end-of-day bar for day d is
    knowable at the close of day d, which is the point-in-time correct knowledge
    time for backfilled OHLCV.
    """
    return df.select(
        pl.col("date").cast(pl.Date),
        pl.col("ticker").alias("symbol").cast(pl.Utf8),
        pl.col("open").cast(pl.Float32),
        pl.col("high").cast(pl.Float32),
        pl.col("low").cast(pl.Float32),
        pl.col("close").cast(pl.Float32),
        pl.col("volume").cast(pl.Float32),
        pl.col("transactions").cast(pl.Int64),
        pl.col("date").cast(pl.Date).alias("as_of_date"),
        pl.lit(source_id).alias("source_id"),
    ).select(list(PRICE_RAW_SCHEMA.keys()))


class PriceIngester:
    def __init__(self, config: AssayConfig, client: LocalFlatFiles | None = None):
        self.config = config
        self.client = client or LocalFlatFiles(config.massive)

    def run(
        self,
        start: dt.date,
        end: dt.date,
        symbols: set[str] | None = None,
    ) -> dict:
        """Transfer + normalize local day aggregates for ``[start, end]``.

        Returns a stats dict: files seen, files written, total rows.
        """
        files = self.client.list_day_aggs(start, end)
        log.info("price transfer: %d day-aggregate files in %s..%s", len(files), start, end)
        if not files:
            log.warning(
                "no day-aggregate files found under %s for %s..%s — is the local "
                "MASSIVE mirror downloaded and MASSIVE_DATA_DIR set correctly?",
                self.client.root, start, end,
            )

        # Group files by (year, month) so each partition is written once.
        by_month: dict[tuple[int, int], list[DayAggFile]] = defaultdict(list)
        for f in files:
            by_month[(f.date.year, f.date.month)].append(f)

        stats = {
            "files_seen": len(files),
            "files_loaded": 0,
            "rows": 0,
            "partitions": 0,
        }
        for (year, month), month_files in sorted(by_month.items()):
            frames: list[pl.DataFrame] = []
            for f in sorted(month_files, key=lambda x: x.date):
                raw = self.client.read_day_agg(f.date, symbols=symbols)
                if raw is None or raw.is_empty():
                    continue
                frames.append(normalize_day_agg(raw, source_id=f.key))
                stats["files_loaded"] += 1
            if not frames:
                continue
            month_df = pl.concat(frames, how="vertical")
            path = price_partition_path(self.config.data_dir, self.config.market, year, month)
            n = upsert_parquet(path, month_df, keys=["date", "symbol"], sort_by=["date", "symbol"])
            stats["rows"] += month_df.height
            stats["partitions"] += 1
            log.info("wrote %s (%d rows this batch, %d total)", path, month_df.height, n)

        return stats
