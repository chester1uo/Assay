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
    TRADE_STATUS_SCHEMA,
    adj_events_path,
    price_partition_path,
    security_groups_path,
    trade_status_path,
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

    def cache_token(self, period):
        """Local file revision for panel reuse; this does not freeze vendor data.

        Includes missing partitions so adding data invalidates an existing panel.
        ctime/inode also catch replacement with preserved modification times.
        """
        from pathlib import Path
        start = _as_date(period[0]) - dt.timedelta(days=_DIV_LOOKBACK_DAYS)
        end = _as_date(period[1])
        root, market = Path(self.config.data_dir), self.config.market
        paths = [price_partition_path(root, market, y, m) for y, m in _months_in_range(start, end)]
        paths += [universe_snapshots_path(root, market), adj_events_path(root, market),
                  security_groups_path(root, market), trade_status_path(root, market)]
        rows = []
        for path in paths:
            try:
                stat = path.stat()
                revision = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            except FileNotFoundError:
                revision = None
            rows.append((str(path.resolve()), revision))
        return (market, tuple(rows))

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
    def _universe_changes(self, index_id, end, as_of_date):
        path = universe_snapshots_path(self.config.data_dir, self.config.market)
        return (pl.read_parquet(path).filter((pl.col('index_id') == index_id.upper()) &
                (pl.col('effective_date') <= _as_date(end)) &
                (pl.col('as_of_date') <= _as_date(as_of_date)))
                .sort(['effective_date', 'as_of_date']).unique(subset=['effective_date'], keep='last')
                .sort('effective_date').to_dicts())

    def universe_span(self, index_id, start, end, as_of_date):
        """Union of members during the period, including securities removed later."""
        changes = self._universe_changes(index_id, end, as_of_date)
        initial = [r for r in changes if r['effective_date'] <= _as_date(start)]
        relevant = initial[-1:] + [r for r in changes if r['effective_date'] > _as_date(start)]
        return sorted({s for r in relevant for s in r['symbols']})

    def get_membership(self, index_id, dates, symbols, as_of_date):
        """Date-aligned membership under the requested knowledge cutoff."""
        import numpy as np
        days = np.asarray(dates).astype('datetime64[D]')
        out = np.zeros((len(days), len(symbols)), dtype=bool)
        if not len(days):
            return out
        changes = self._universe_changes(index_id, str(days[-1]), as_of_date)
        for i, row in enumerate(changes):
            rows = days >= np.datetime64(row['effective_date'])
            if i + 1 < len(changes):
                rows &= days < np.datetime64(changes[i+1]['effective_date'])
            if rows.any():
                out[rows] = np.isin(symbols, row['symbols'])
        return out

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

    # -- trade status (price-limit bands / tradability) -----------------------
    def get_trade_status(
        self, symbols, start_date, end_date, as_of_date
    ) -> pl.DataFrame:
        """Per ``(date, symbol)`` price-limit bands + locked flags, PIT-filtered.

        Returns columns ``date, symbol, up_limit, down_limit, limit_up_locked,
        limit_down_locked, close`` (the raw close, for basis rebasing). Markets
        without a ``trade_status`` store (e.g. US) return an **empty** frame
        rather than raising, so a caller can treat "no limit data" as "no limit
        constraint" and degrade gracefully.
        """
        path = trade_status_path(self.config.data_dir, self.config.market)
        if not path.is_file():
            return pl.DataFrame(schema=TRADE_STATUS_SCHEMA)
        start = _as_date(start_date)
        end = _as_date(end_date)
        as_of = _as_date(as_of_date)
        lf = pl.scan_parquet(str(path)).filter(
            (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & (pl.col("as_of_date") <= as_of)
        )
        if symbols is not None:
            lf = lf.filter(pl.col("symbol").is_in(list(symbols)))
        return lf.collect()

    # -- security groups (industry / sector labels) ---------------------------
    def get_groups(self, symbols, as_of_date) -> dict[str, str]:
        """Map ``symbol -> group`` (industry/sector), PIT-filtered by ``as_of_date``.

        Returns ``{}`` (not an error) when the market has no ``security_groups``
        store, so a caller can treat "no group data" as "no neutralization". One
        label per symbol (latest knowable as-of the query date).
        """
        path = security_groups_path(self.config.data_dir, self.config.market)
        if not path.is_file():
            return {}
        as_of = _as_date(as_of_date)
        df = pl.read_parquet(path).filter(pl.col("as_of_date") <= as_of)
        if symbols is not None:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        if df.is_empty():
            return {}
        df = df.sort("as_of_date").unique(subset=["symbol"], keep="last")
        return dict(zip(df["symbol"].to_list(), df["group"].to_list()))

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
