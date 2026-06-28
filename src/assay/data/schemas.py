"""Canonical schemas and on-disk layout for the Assay data stores.

Three parquet stores back the data layer (see engineering docs section 3.2):

* ``price_raw``           — unadjusted daily OHLCV, partitioned by year/month.
* ``adj_events``          — corporate-action log (splits, dividends, mergers).
* ``universe_snapshots``  — index membership history (NASDAQ-100, ...).

Schemas are expressed as polars dtypes so the ingesters can build/validate
frames and the store can read them back with stable types.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

# --- raw MASSIVE flat-file (day aggregate) CSV layout, in delivery order ------
DAY_AGG_CSV_COLUMNS: tuple[str, ...] = (
    "ticker",
    "volume",
    "open",
    "close",
    "high",
    "low",
    "window_start",  # Unix nanoseconds, start of the trading-day window (ET)
    "transactions",
)

# --- price_raw ---------------------------------------------------------------
# date/as_of_date are bi-temporal: `date` is the trading day (event_time) and
# `as_of_date` is when the row became knowable. For end-of-day OHLCV the price
# for day d is known at the close of day d, so as_of_date == date.
PRICE_RAW_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "symbol": pl.Utf8,
    "open": pl.Float32,
    "high": pl.Float32,
    "low": pl.Float32,
    "close": pl.Float32,  # unadjusted close
    "volume": pl.Float32,
    "transactions": pl.Int64,
    "as_of_date": pl.Date,  # knowledge_time
    "source_id": pl.Utf8,  # provenance: flat-file object key
}

# --- adj_events --------------------------------------------------------------
# One row per corporate action. The cumulative point-in-time adjustment factor
# is derived at read time from `split_ratio` and `dividend_cash` (never trusted
# from the provider's "as-of-today" value, which is kept only for reference).
ADJ_EVENTS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "ex_date": pl.Date,  # event_time: ex-dividend / split execution date
    "as_of_date": pl.Date,  # knowledge_time: when the action became knowable
    "event_type": pl.Utf8,  # SPLIT | REVERSE_SPLIT | DIVIDEND | MERGER | SPINOFF | ...
    "split_ratio": pl.Float64,  # forward ratio split_to/split_from; 1.0 if not a share-change
    "dividend_cash": pl.Float64,  # raw cash dividend amount; 0.0 if not a dividend
    "provider_adj_factor": pl.Float64,  # MASSIVE historical_adjustment_factor (reference only)
    "event_id": pl.Utf8,  # provider event id (dedup key)
    "source": pl.Utf8,  # 'massive:splits' | 'massive:dividends'
}

# --- universe_snapshots ------------------------------------------------------
UNIVERSE_SNAPSHOTS_SCHEMA: dict[str, pl.DataType] = {
    "index_id": pl.Utf8,  # e.g. NASDAQ100
    "effective_date": pl.Date,  # date this composition became active
    "symbols": pl.List(pl.Utf8),  # constituent tickers on that date
    "as_of_date": pl.Date,  # knowledge_time
}

# --- trade_status (market-microstructure tradability) ------------------------
# Per (date, symbol) trading constraints that a backtest's execution layer needs
# but that are *not* prices: the daily up/down price-limit bands (涨跌停价). Only
# populated for markets that have such rules (A-share). ``close`` is the *raw*
# (unadjusted) close on that bar, kept so the reader can rebase the raw limit
# bands into whatever adjusted basis the price panel uses (limit prices are raw;
# the panel may be split/total-adjusted — comparing the two requires the factor
# ``adj_close / close``). Suspension is *not* stored here: a suspended name simply
# has no price_raw bar on that date, which the execution layer already reads as
# untradable (NaN price).
TRADE_STATUS_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "symbol": pl.Utf8,
    "up_limit": pl.Float64,  # daily ceiling price (涨停价), raw
    "down_limit": pl.Float64,  # daily floor price (跌停价), raw
    "limit_up_locked": pl.Boolean,  # low >= up_limit: locked at ceiling all day (unbuyable)
    "limit_down_locked": pl.Boolean,  # high <= down_limit: locked at floor (unsellable)
    "close": pl.Float64,  # raw (unadjusted) close, for basis rebasing
    "as_of_date": pl.Date,  # knowledge_time (== date for EOD bands)
}

# --- security_groups (classification for neutralization) ---------------------
# Per-symbol group label (industry / sector) used by sector-neutral signal
# processing and the sector-weight constraint. A *current snapshot* in practice
# (most providers expose only today's industry) — ``as_of_date`` is set to the
# listing date so the label is "known since listing"; the value itself is not
# point-in-time-versioned (documented limitation, like a survivorship snapshot).
SECURITY_GROUPS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "group": pl.Utf8,  # classification label (e.g. Tushare/SW industry)
    "as_of_date": pl.Date,  # knowledge_time
}


# --- on-disk paths -----------------------------------------------------------
def price_partition_path(data_dir: Path, market: str, year: int, month: int) -> Path:
    """Parquet path for one month of price_raw data."""
    return (
        Path(data_dir)
        / "price_raw"
        / f"market={market}"
        / f"year={year:04d}"
        / f"month={month:02d}"
        / "price_raw.parquet"
    )


def price_root(data_dir: Path, market: str) -> Path:
    return Path(data_dir) / "price_raw" / f"market={market}"


def adj_events_path(data_dir: Path, market: str) -> Path:
    return Path(data_dir) / "adj_events" / f"market={market}" / "adj_events.parquet"


def universe_snapshots_path(data_dir: Path, market: str) -> Path:
    return Path(data_dir) / "universe_snapshots" / f"market={market}" / "universe_snapshots.parquet"


def trade_status_path(data_dir: Path, market: str) -> Path:
    return Path(data_dir) / "trade_status" / f"market={market}" / "trade_status.parquet"


def security_groups_path(data_dir: Path, market: str) -> Path:
    return Path(data_dir) / "security_groups" / f"market={market}" / "security_groups.parquet"
