"""Ingest the raw Tushare mirror into Assay's canonical stores (``market=CN``).

Reads the per-symbol raw parquet produced by ``scripts/download_tushare.py``
(default ``/data/tushare_data``) and writes the *same* canonical stores the
US/MASSIVE pipeline produces, so :class:`~assay.data.store.datastore.DataStore`,
:class:`~assay.engine.FactorEngine` and the portfolio backtester operate on
A-share data with no engine changes:

================  =========================  ===========================================
canonical store   source (tushare raw)       mapping
================  =========================  ===========================================
``price_raw``     ``cn/daily``               unadjusted OHLCV (vol in 手/lots)
``adj_events``    ``cn/dividend``            送转 -> ``split_ratio``; 税前现金分红 -> ``dividend_cash``
``universe_...``  ``cn/index_weight``        CSI300/500/1000 PIT monthly membership
``trade_status``  ``cn/stk_limit`` + daily   raw up/down limit bands + locked flags
================  =========================  ===========================================

Everything is written under ``market="CN"``. Suspension needs no store: a halted
name simply has no ``price_raw`` bar, which the execution layer reads as NaN /
untradable.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import polars as pl

from assay.data.io_utils import upsert_parquet, write_parquet_atomic
from assay.data.schemas import (
    ADJ_EVENTS_SCHEMA,
    PRICE_RAW_SCHEMA,
    SECURITY_GROUPS_SCHEMA,
    TRADE_STATUS_SCHEMA,
    UNIVERSE_SNAPSHOTS_SCHEMA,
    adj_events_path,
    price_partition_path,
    security_groups_path,
    trade_status_path,
    universe_snapshots_path,
)

log = logging.getLogger("assay.tushare.ingest")

CN_MARKET = "CN"
DEFAULT_TUSHARE_DIR = "/data/tushare_data"
CN_INDICES = ("CSI300", "CSI500", "CSI1000")

# Limit-locked tolerances: a bar that never traded below its ceiling (low >=
# up_limit) was locked limit-up all session (unbuyable); symmetric for the floor.
_LOCK_TOL = 0.999


def tushare_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get("TUSHARE_DATA_DIR", DEFAULT_TUSHARE_DIR)).expanduser()


def _existing(paths: list[Path]) -> list[str]:
    return [str(p) for p in paths if p.is_file()]


def _ymd(col: str) -> pl.Expr:
    """Parse a Tushare ``YYYYMMDD`` string column into a polars ``Date``."""
    return pl.col(col).cast(pl.Utf8).str.strptime(pl.Date, "%Y%m%d", strict=False)


def universe_symbols(tdir: Path) -> list[str]:
    """The deduped A-share union (ts_codes) the downloader wrote, sorted."""
    upath = tdir / "cn" / "universe.parquet"
    if not upath.is_file():
        raise FileNotFoundError(
            f"no {upath}; run scripts/download_tushare.py (cn_universe) first"
        )
    return sorted(pl.read_parquet(upath)["ts_code"].to_list())


# --------------------------------------------------------------------------- #
# price_raw
# --------------------------------------------------------------------------- #
def ingest_cn_prices(
    tdir: Path, data_dir: Path, symbols: list[str], start: dt.date, end: dt.date
) -> dict:
    """``cn/daily`` raw OHLCV -> month-partitioned ``price_raw`` (market=CN)."""
    files = _existing([tdir / "cn" / "daily" / f"{s}.parquet" for s in symbols])
    if not files:
        return {"rows": 0, "partitions": 0, "note": "no cn/daily files"}
    raw = pl.scan_parquet(files).collect()
    if raw.is_empty():
        return {"rows": 0, "partitions": 0}
    px = (
        raw.select(
            _ymd("trade_date").alias("date"),
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            pl.col("open").cast(pl.Float32),
            pl.col("high").cast(pl.Float32),
            pl.col("low").cast(pl.Float32),
            pl.col("close").cast(pl.Float32),
            pl.col("vol").alias("volume").cast(pl.Float32),  # Tushare vol is in 手 (100-share lots)
            pl.lit(None).cast(pl.Int64).alias("transactions"),  # not provided by Tushare daily
        )
        .drop_nulls(["date"])
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .with_columns(
            pl.col("date").alias("as_of_date"),  # EOD bar knowable same day
            pl.lit("tushare:daily").alias("source_id"),
        )
        .select(list(PRICE_RAW_SCHEMA.keys()))
    )
    rows = 0
    parts = 0
    for (year, month), part in px.with_columns(
        pl.col("date").dt.year().alias("_y"), pl.col("date").dt.month().alias("_m")
    ).group_by(["_y", "_m"]):
        part = part.drop(["_y", "_m"])
        path = price_partition_path(data_dir, CN_MARKET, int(year), int(month))
        upsert_parquet(path, part, keys=["date", "symbol"], sort_by=["date", "symbol"])
        rows += part.height
        parts += 1
    log.info("cn price_raw: %d rows across %d month partitions", rows, parts)
    return {"rows": rows, "partitions": parts}


# --------------------------------------------------------------------------- #
# adj_events
# --------------------------------------------------------------------------- #
def ingest_cn_adj_events(tdir: Path, data_dir: Path, symbols: list[str]) -> dict:
    """``cn/dividend`` -> ``adj_events`` (market=CN).

    Only *implemented* dividends (``div_proc == '实施'``) with an ex-date become
    events. 送转 (``stk_div`` shares-per-share) maps to a forward ``split_ratio``
    of ``1 + stk_div``; the pre-tax cash dividend (``cash_div_tax``, falling back
    to ``cash_div``) maps to ``dividend_cash``. One record can carry both.
    """
    files = _existing([tdir / "cn" / "dividend" / f"{s}.parquet" for s in symbols])
    if not files:
        return {"rows": 0, "note": "no cn/dividend files"}
    div = pl.scan_parquet(files).collect()
    if div.is_empty():
        return {"rows": 0}

    cash_tax = pl.col("cash_div_tax").cast(pl.Float64).fill_null(0.0)
    cash_net = pl.col("cash_div").cast(pl.Float64).fill_null(0.0)
    stk = pl.col("stk_div").cast(pl.Float64).fill_null(0.0)

    ev = (
        div.filter((pl.col("div_proc") == "实施") & pl.col("ex_date").is_not_null())
        .select(
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            _ymd("ex_date").alias("ex_date"),
            pl.coalesce([_ymd("imp_ann_date"), _ymd("ann_date"), _ymd("ex_date")]).alias("as_of_date"),
            pl.when(stk > 0).then(pl.lit("SPLIT")).otherwise(pl.lit("DIVIDEND")).alias("event_type"),
            (1.0 + stk).alias("split_ratio"),
            pl.when(cash_tax > 0).then(cash_tax).otherwise(cash_net).alias("dividend_cash"),
            pl.lit(0.0).alias("provider_adj_factor"),
            (pl.lit("tushare:div:") + pl.col("ts_code") + ":" + pl.col("ex_date").cast(pl.Utf8)).alias("event_id"),
            pl.lit("tushare:dividend").alias("source"),
        )
        .drop_nulls(["ex_date"])
        # keep only economically-meaningful events (a share change or a cash payout)
        .filter((pl.col("split_ratio") != 1.0) | (pl.col("dividend_cash") > 0.0))
        .unique(subset=["event_id"], keep="last")
        .select(list(ADJ_EVENTS_SCHEMA.keys()))
    )
    if ev.is_empty():
        return {"rows": 0}
    path = adj_events_path(data_dir, CN_MARKET)
    n = upsert_parquet(path, ev, keys=["event_id"], sort_by=["symbol", "ex_date"])
    log.info("cn adj_events: %d new events, %d total", ev.height, n)
    return {"rows": ev.height, "total": n}


# --------------------------------------------------------------------------- #
# universe_snapshots
# --------------------------------------------------------------------------- #
def _membership_snapshots(iw: pl.DataFrame) -> list[tuple[dt.date, list[str]]]:
    """Collapse an ``index_weight`` frame to (effective_date, members) snapshots.

    One snapshot per *composition change*: consecutive trade-dates with an
    identical constituent set are de-duplicated (membership only moves at
    rebalances, but the early CSI300 weights are daily).
    """
    by_date = (
        iw.group_by("trade_date")
        .agg(pl.col("con_code"))
        .sort("trade_date")
    )
    out: list[tuple[dt.date, list[str]]] = []
    prev: frozenset[str] | None = None
    for date_str, cons in zip(by_date["trade_date"], by_date["con_code"]):
        members = frozenset(cons)
        if members and members != prev:
            d = dt.datetime.strptime(str(date_str), "%Y%m%d").date()
            out.append((d, sorted(members)))
            prev = members
    return out


def ingest_cn_universe(tdir: Path, data_dir: Path, indices=CN_INDICES) -> dict:
    """``cn/index_weight/{INDEX}.parquet`` -> ``universe_snapshots`` (market=CN)."""
    rows: list[dict] = []
    per_index: dict[str, int] = {}
    for idx in indices:
        path = tdir / "cn" / "index_weight" / f"{idx}.parquet"
        if not path.is_file():
            log.warning("cn universe: missing %s, skipping", path)
            continue
        iw = pl.read_parquet(path)
        if iw.is_empty():
            continue
        snaps = _membership_snapshots(iw)
        per_index[idx] = len(snaps)
        for eff, members in snaps:
            rows.append(
                {"index_id": idx, "effective_date": eff, "symbols": members, "as_of_date": eff}
            )
    if not rows:
        return {"snapshots": 0}
    df = pl.DataFrame(rows, schema=UNIVERSE_SNAPSHOTS_SCHEMA)
    out = universe_snapshots_path(data_dir, CN_MARKET)
    n = upsert_parquet(
        out, df, keys=["index_id", "effective_date"], sort_by=["index_id", "effective_date"]
    )
    log.info("cn universe_snapshots: %s (%d total rows)", per_index, n)
    return {"snapshots": len(rows), "per_index": per_index, "total": n}


# --------------------------------------------------------------------------- #
# trade_status (price-limit bands + locked flags)
# --------------------------------------------------------------------------- #
def ingest_cn_trade_status(
    tdir: Path, data_dir: Path, symbols: list[str], start: dt.date, end: dt.date
) -> dict:
    """``cn/stk_limit`` (+ daily high/low/close) -> ``trade_status`` (market=CN)."""
    lim_files = _existing([tdir / "cn" / "stk_limit" / f"{s}.parquet" for s in symbols])
    day_files = _existing([tdir / "cn" / "daily" / f"{s}.parquet" for s in symbols])
    if not lim_files:
        return {"rows": 0, "note": "no cn/stk_limit files — run the cn_limit download"}
    lim = (
        pl.scan_parquet(lim_files)
        .select(
            _ymd("trade_date").alias("date"),
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            pl.col("up_limit").cast(pl.Float64),
            pl.col("down_limit").cast(pl.Float64),
        )
        .collect()
    )
    day = (
        pl.scan_parquet(day_files)
        .select(
            _ymd("trade_date").alias("date"),
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
        )
        .collect()
    )
    ts = (
        lim.join(day, on=["date", "symbol"], how="left")
        .drop_nulls(["date"])
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .with_columns(
            (
                pl.col("up_limit").is_not_null()
                & pl.col("low").is_not_null()
                & (pl.col("low") >= pl.col("up_limit") * _LOCK_TOL)
            ).alias("limit_up_locked"),
            (
                pl.col("down_limit").is_not_null()
                & pl.col("high").is_not_null()
                & (pl.col("high") <= pl.col("down_limit") / _LOCK_TOL)
            ).alias("limit_down_locked"),
            pl.col("date").alias("as_of_date"),
        )
        .select(list(TRADE_STATUS_SCHEMA.keys()))
    )
    if ts.is_empty():
        return {"rows": 0}
    path = trade_status_path(data_dir, CN_MARKET)
    # Single store (read lazily + predicate-pushed by the DataStore). Fresh build
    # writes directly; a re-run upserts on (date, symbol).
    n = upsert_parquet(path, ts, keys=["date", "symbol"], sort_by=["date", "symbol"])
    locked_up = int(ts["limit_up_locked"].sum())
    locked_dn = int(ts["limit_down_locked"].sum())
    log.info("cn trade_status: %d rows (%d locked-up, %d locked-down), %d total",
             ts.height, locked_up, locked_dn, n)
    return {"rows": ts.height, "locked_up": locked_up, "locked_down": locked_dn, "total": n}


# --------------------------------------------------------------------------- #
# security_groups (industry classification for neutralization)
# --------------------------------------------------------------------------- #
def ingest_cn_groups(tdir: Path, data_dir: Path) -> dict:
    """``meta/stock_basic`` industry -> ``security_groups`` (market=CN).

    One label per ts_code (Tushare's ``industry`` field). ``as_of_date`` is the
    listing date (the label is "known since listing"); the industry *value* is a
    current snapshot, not point-in-time-versioned — documented in the schema.
    """
    sb_path = tdir / "meta" / "stock_basic.parquet"
    if not sb_path.is_file():
        return {"rows": 0, "note": "no meta/stock_basic.parquet"}
    sb = pl.read_parquet(sb_path)
    if sb.is_empty():
        return {"rows": 0}
    grp = (
        sb.select(
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            pl.col("industry").cast(pl.Utf8).fill_null("UNKNOWN").alias("group"),
            _ymd("list_date").alias("as_of_date"),
        )
        .drop_nulls(["symbol"])
        # listing date can be null/未上市 for a few rows — make the label always knowable
        .with_columns(pl.col("as_of_date").fill_null(dt.date(1990, 1, 1)))
        .unique(subset=["symbol"], keep="first")
        .select(list(SECURITY_GROUPS_SCHEMA.keys()))
    )
    path = security_groups_path(data_dir, CN_MARKET)
    n = upsert_parquet(path, grp, keys=["symbol"], sort_by=["symbol"])
    log.info("cn security_groups: %d symbols, %d industries", grp.height, grp["group"].n_unique())
    return {"rows": grp.height, "industries": int(grp["group"].n_unique()), "total": n}


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def prepare_cn(
    data_dir: str | Path,
    *,
    start: dt.date,
    end: dt.date,
    tushare_data_dir: str | Path | None = None,
    indices=CN_INDICES,
    symbols: list[str] | None = None,
    do_universe: bool = True,
    do_prices: bool = True,
    do_adj: bool = True,
    do_trade_status: bool = True,
    do_groups: bool = True,
) -> dict:
    """Ingest the raw Tushare CN mirror into the canonical stores under ``data_dir``.

    ``symbols`` defaults to the full survivorship-free union the downloader wrote
    (``cn/universe.parquet``). Produces ``price_raw`` / ``adj_events`` /
    ``universe_snapshots`` / ``trade_status`` partitions for ``market=CN`` that a
    ``DataStore(AssayConfig(market="CN", data_dir=...))`` reads directly.
    """
    tdir = tushare_dir(tushare_data_dir)
    data_dir = Path(data_dir).expanduser()
    if symbols is None:
        symbols = universe_symbols(tdir)
    report: dict = {
        "market": CN_MARKET,
        "data_dir": str(data_dir),
        "n_symbols": len(symbols),
        "range": [start.isoformat(), end.isoformat()],
    }
    log.info("prepare_cn: %d symbols, %s..%s -> %s", len(symbols), start, end, data_dir)
    if do_universe:
        report["universe"] = ingest_cn_universe(tdir, data_dir, indices)
    if do_prices:
        report["prices"] = ingest_cn_prices(tdir, data_dir, symbols, start, end)
    if do_adj:
        report["adj_events"] = ingest_cn_adj_events(tdir, data_dir, symbols)
    if do_trade_status:
        report["trade_status"] = ingest_cn_trade_status(tdir, data_dir, symbols, start, end)
    if do_groups:
        report["groups"] = ingest_cn_groups(tdir, data_dir)
    return report
