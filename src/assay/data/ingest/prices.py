"""Price ingester: MASSIVE day-aggregate flat files -> ``price_raw`` parquet.

Downloads day-aggregate CSVs over a date range, optionally filtered to a symbol
universe, normalizes them to the ``price_raw`` schema, and writes month
partitions (``market=US/year=YYYY/month=MM/price_raw.parquet``).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import polars as pl

from assay.config import AssayConfig
from assay.data.io_utils import upsert_parquet
from assay.data.massive.flatfiles import DayAggFile, FlatFilesClient, FlatFilesForbidden
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
    def __init__(self, config: AssayConfig, client: FlatFilesClient | None = None):
        self.config = config
        self.client = client or FlatFilesClient(config.massive)

    def run(
        self,
        start: dt.date,
        end: dt.date,
        symbols: set[str] | None = None,
    ) -> dict:
        """Download + normalize day aggregates for ``[start, end]``.

        Returns a stats dict: files seen, files written, total rows.
        """
        files = self.client.list_day_aggs(start, end)
        log.info("price ingest: %d day-aggregate files in %s..%s", len(files), start, end)

        # Group files by (year, month) so each partition is written once.
        by_month: dict[tuple[int, int], list[DayAggFile]] = defaultdict(list)
        for f in files:
            by_month[(f.date.year, f.date.month)].append(f)

        stats = {
            "files_seen": len(files),
            "files_loaded": 0,
            "files_forbidden": 0,
            "rows": 0,
            "partitions": 0,
        }
        forbidden_dates: list[dt.date] = []
        for (year, month), month_files in sorted(by_month.items()):
            frames: list[pl.DataFrame] = []
            for f in sorted(month_files, key=lambda x: x.date):
                try:
                    raw = self.client.read_day_agg(f.date, symbols=symbols)
                except FlatFilesForbidden:
                    stats["files_forbidden"] += 1
                    forbidden_dates.append(f.date)
                    continue
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

        if forbidden_dates:
            log.warning(
                "%d/%d files were 403 Forbidden (outside your MASSIVE subscription "
                "window), e.g. %s .. %s — these dates were skipped.",
                stats["files_forbidden"], stats["files_seen"],
                min(forbidden_dates), max(forbidden_dates),
            )
        if stats["files_loaded"] == 0 and stats["files_forbidden"] > 0:
            raise RuntimeError(
                f"All {stats['files_forbidden']} requested day-aggregate files returned 403 "
                "Forbidden. The entire date range is outside your MASSIVE subscription window "
                "(downloads are entitled on a rolling window even though the bucket lists "
                "further back). Try a more recent --start date."
            )
        return stats
