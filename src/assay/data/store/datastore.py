"""Point-in-time read interface over the prepared parquet stores.

Every query takes an explicit ``as_of_date`` so look-ahead bias is structurally
impossible: prices, corporate actions, and universe membership are all filtered
to what was knowable on that date (engineering docs section 3.4).
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from assay.config import AssayConfig
from assay.data.schemas import (
    ADJ_EVENTS_SCHEMA,
    adj_events_path,
    price_partition_path,
    universe_snapshots_path,
)
from assay.data.store.adjust import forward_adjust

log = logging.getLogger(__name__)

_PRICE_FIELDS = {"open", "high", "low", "close", "volume", "transactions"}

# Calendar-day lead-in read before `start` so the raw close on the trading day
# immediately before an early ex-dividend is available for the dividend factor.
_DIV_LOOKBACK_DAYS = 10


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _months_in_range(start: dt.date, end: dt.date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            month, year = 1, year + 1


class DataStore:
    def __init__(self, config: AssayConfig | None = None):
        self.config = config or AssayConfig.from_env()

    # -- universe -------------------------------------------------------------
    def get_universe(self, index_id: str, date, as_of_date) -> list[str]:
        """Constituents of ``index_id`` effective on ``date``, known by ``as_of_date``."""
        date = _as_date(date)
        as_of_date = _as_date(as_of_date)
        path = universe_snapshots_path(self.config.data_dir, self.config.market)
        if not path.is_file():
            raise FileNotFoundError(
                f"no universe_snapshots at {path}; run the universe ingester first"
            )
        snap = (
            pl.read_parquet(path)
            .filter(
                (pl.col("index_id") == index_id.upper())
                & (pl.col("effective_date") <= date)
                & (pl.col("as_of_date") <= as_of_date)
            )
            .sort("effective_date", descending=True)
            .head(1)
        )
        if snap.is_empty():
            return []
        return list(snap["symbols"][0])

    # -- raw reads ------------------------------------------------------------
    def _read_prices(
        self, symbols, start: dt.date, end: dt.date, as_of_date: dt.date
    ) -> pl.DataFrame:
        paths = [
            p
            for y, m in _months_in_range(start, end)
            if (p := price_partition_path(self.config.data_dir, self.config.market, y, m)).is_file()
        ]
        if not paths:
            raise FileNotFoundError(
                f"no price_raw partitions for {start}..{end} under "
                f"{self.config.data_dir}; run the price ingester first"
            )
        lf = pl.scan_parquet([str(p) for p in paths]).filter(
            (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & (pl.col("as_of_date") <= as_of_date)
        )
        if symbols is not None:
            lf = lf.filter(pl.col("symbol").is_in(list(symbols)))
        return lf.collect()

    def _read_adj_events(self, symbols, end: dt.date, as_of_date: dt.date) -> pl.DataFrame:
        path = adj_events_path(self.config.data_dir, self.config.market)
        if not path.is_file():
            return pl.DataFrame(schema=ADJ_EVENTS_SCHEMA)
        df = pl.read_parquet(path).filter(
            (pl.col("ex_date") <= end) & (pl.col("as_of_date") <= as_of_date)
        )
        if symbols is not None:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        return df

    # -- panel ----------------------------------------------------------------
    def get_panel(
        self,
        fields: list[str],
        symbols,
        start_date,
        end_date,
        as_of_date,
        adj: str = "split",
    ) -> pl.DataFrame:
        """Return a point-in-time correct, corporate-action-adjusted price panel.

        Parameters mirror the engineering-doc signature. ``as_of_date`` is
        required. ``adj`` is ``none`` | ``split`` | ``total`` (alias ``forward``).
        The result is a long frame with columns ``date``, ``symbol``, ``*fields``.
        """
        if as_of_date is None:
            raise ValueError("as_of_date is required (point-in-time correctness).")
        start = _as_date(start_date)
        end = _as_date(end_date)
        as_of = _as_date(as_of_date)

        unknown = [f for f in fields if f not in _PRICE_FIELDS]
        if unknown:
            raise ValueError(
                f"unsupported field(s) {unknown}; day aggregates provide {sorted(_PRICE_FIELDS)} "
                "(vwap is not available from this source)."
            )

        syms = sorted(set(symbols)) if symbols is not None else None
        # Read a short lead-in before `start` so a dividend whose ex-date falls
        # early in the window can find the raw close on the prior session.
        read_start = start - dt.timedelta(days=_DIV_LOOKBACK_DAYS)
        prices = self._read_prices(syms, read_start, end, as_of)
        if prices.is_empty():
            return pl.DataFrame(schema={"date": pl.Date, "symbol": pl.Utf8})

        # Forward-adjust onto the basis of `end` (the latest in-window bar): only
        # actions effective by `end` affect the window, and only those knowable by
        # `as_of` are used — so no future action can leak into a past query, and
        # the result does not depend on how far past `end` `as_of` sits.
        events = self._read_adj_events(syms, end, as_of)
        adjusted = forward_adjust(prices, events, mode=adj)

        return (
            adjusted.filter((pl.col("date") >= start) & (pl.col("date") <= end))
            .select(["date", "symbol", *fields])
            .sort(["date", "symbol"])
        )
