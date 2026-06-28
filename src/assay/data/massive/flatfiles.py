"""Local reader for MASSIVE US-stock daily aggregates.

Reads day-aggregate OHLCV bars from the locally-downloaded MASSIVE mirror
instead of fetching them from S3. On-disk layout (mirrors the bucket)::

    {day_aggs_dir}/{YYYY}/{MM}/{YYYY-MM-DD}.parquet

Each parquet has the MASSIVE flat-file columns::

    ticker, volume, open, close, high, low, window_start, transactions

``window_start`` is the Unix-nanosecond timestamp of the start of the trading
day window (midnight US/Eastern), so the trading date is recovered by converting
to ``America/New_York``.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from assay.config import MassiveConfig
from assay.data.schemas import DAY_AGG_CSV_COLUMNS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DayAggFile:
    """A located day-aggregate object: its trading date, path, and source key."""

    date: dt.date
    path: Path
    key: str  # path relative to the source root — used as price_raw.source_id


class LocalFlatFiles:
    """Reader over the locally-downloaded MASSIVE day-aggregate parquet files."""

    def __init__(self, config: MassiveConfig):
        self.config = config
        self.root = config.day_aggs_dir

    # -- path helpers ---------------------------------------------------------
    def day_agg_path(self, date: dt.date) -> Path:
        return self.root / f"{date:%Y}" / f"{date:%m}" / f"{date:%Y-%m-%d}.parquet"

    def _rel_key(self, path: Path) -> str:
        """Path relative to the source root, for stable provenance ids."""
        try:
            return str(path.relative_to(self.config.source_dir))
        except ValueError:
            return str(path)

    # -- discovery ------------------------------------------------------------
    def list_top_level_prefixes(self) -> list[str]:
        """List the dataset directories present under the source root (discovery)."""
        src = Path(self.config.source_dir)
        if not src.is_dir():
            return []
        return sorted(p.name + "/" for p in src.iterdir() if p.is_dir())

    def list_day_aggs(self, start: dt.date, end: dt.date) -> list[DayAggFile]:
        """List day-aggregate files whose trading date falls in ``[start, end]``.

        Listing the actual files (rather than probing each calendar date) means
        holidays/non-trading days simply don't appear — no guessing required.
        """
        found: list[DayAggFile] = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            mdir = self.root / f"{year:04d}" / f"{month:02d}"
            if mdir.is_dir():
                for path in mdir.iterdir():
                    date = self._date_from_path(path)
                    if date is not None and start <= date <= end:
                        found.append(
                            DayAggFile(date=date, path=path, key=self._rel_key(path))
                        )
            month += 1
            if month > 12:
                month, year = 1, year + 1
        found.sort(key=lambda f: f.date)
        return found

    @staticmethod
    def _date_from_path(path: Path) -> dt.date | None:
        if path.suffix != ".parquet":
            return None
        try:
            return dt.date.fromisoformat(path.stem)
        except ValueError:
            return None

    # -- read -----------------------------------------------------------------
    def read_day_agg(
        self, date: dt.date, symbols: set[str] | None = None
    ) -> pl.DataFrame | None:
        """Read one local day-aggregate parquet into a typed polars frame.

        Returns ``None`` when no file exists for ``date`` (e.g. a holiday or a
        date outside the downloaded range). When ``symbols`` is given, rows are
        filtered to that set. The returned frame keeps the raw provider columns
        plus a derived ``date`` (ET trading date); normalization to the
        ``price_raw`` schema happens in the ingester.
        """
        path = self.day_agg_path(date)
        if not path.is_file():
            return None

        df = pl.read_parquet(path, columns=list(DAY_AGG_CSV_COLUMNS))
        if symbols is not None:
            df = df.filter(pl.col("ticker").is_in(list(symbols)))

        # Derive the ET trading date from the nanosecond window_start.
        df = df.with_columns(
            pl.from_epoch("window_start", time_unit="ns")
            .dt.replace_time_zone("UTC")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("date")
        )
        return df
