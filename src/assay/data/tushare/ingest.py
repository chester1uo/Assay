"""Ingest the raw Tushare mirror into Assay's canonical stores (``market=CN``/``HK``).

Reads the raw parquet produced by ``scripts/download_tushare.py`` (default
``/data/tushare_data``) and writes the *same* canonical stores the US/MASSIVE
pipeline produces, so :class:`~assay.data.store.datastore.DataStore`,
:class:`~assay.engine.FactorEngine` and the portfolio backtester operate on
A-share and Hong Kong data with no engine changes:

================  =========================  ===========================================
canonical store   source (tushare raw)       mapping
================  =========================  ===========================================
``price_raw``     ``cn/daily``               unadjusted OHLCV (vol in 手/lots)
``adj_events``    ``cn/dividend``            送转 -> ``split_ratio``; 税前现金分红 -> ``dividend_cash``
``universe_...``  ``cn/index_weight``        CSI300/500/1000 PIT monthly membership
``trade_status``  ``cn/stk_limit`` + daily   raw up/down limit bands + locked flags
---------------------------------------------------------------------------------------
``price_raw``     ``hk/daily``               unadjusted OHLCV (vol in shares)
``adj_events``    ``hk/adj_factor``          ``cum_adjfactor`` steps, decomposed (below)
``universe_...``  ``hk/daily`` turnover      HK100 point-in-time liquidity universe
================  =========================  ===========================================

Suspension needs no store: a halted name simply has no ``price_raw`` bar, which
the execution layer reads as NaN / untradable. ``trade_status`` is CN-only —
HKEX has no daily price-limit bands. ``security_groups`` is CN-only too: Tushare
exposes no HK industry classification, so HK neutralization is honestly
unavailable rather than silently wrong.

HK corporate actions
--------------------
Tushare gives HK a single *cumulative* factor (``hk_adjfactor``) rather than the
dividend/split event records the CN path enjoys, so events are reconstructed
from its steps: on a date whose factor moves from ``f_prev`` to ``f``, the total
price ratio is ``r = f / f_prev``. That ratio is then attributed to a share
change or a cash distribution using the share count from ``hk/daily_adj``.

The total-return series is exact under either attribution, so the attribution
only moves the split-only view — but that view is the *default* read basis, so
the tie is broken toward a share event: calling a split a dividend would book
half the share price as cash and leave the raw split gap in the panel, which
reads as a fabricated -50% return. See :func:`ingest_hk_adj_events` for the
preconditions this store depends on.
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
HK_MARKET = "HK"
DEFAULT_TUSHARE_DIR = "/data/tushare_data"
CN_INDICES = ("CSI300", "CSI500", "CSI1000")
# HSI/HSTECH membership is a vendored current snapshot (Tushare has no HK
# membership API); HK100 is derived point-in-time from realised turnover and is
# the one to prefer for backtests.
HK_SNAPSHOT_INDICES = ("HSI", "HSTECH")
HK_LIQUID_INDEX = "HK100"
# H-shares: mainland-incorporated issuers listed on HKEX. A separate universe
# because they are a distinct asset class from the red chips and local Hong Kong
# issuers that make up the rest of the board — different regulator, different
# shareholder base, and the A/H pair makes them the natural HK leg of a
# cross-market book.
HK_HSHARE_INDEX = "HSHARE100"
HK_INDICES = (HK_LIQUID_INDEX, HK_HSHARE_INDEX) + HK_SNAPSHOT_INDICES
# ISIN country prefix marking a mainland-China incorporation.
_HK_MAINLAND_ISIN = "CNE"
# Fixed effective date for the vendored HSI/HSTECH snapshots — see
# ingest_hk_universe. Early enough to cover any backtest window.
_HK_SNAPSHOT_EPOCH = dt.date(1990, 1, 1)

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
def cn_daily_files(tdir: Path, symbols: list[str], start: dt.date, end: dt.date) -> list[str]:
    """Read the daily market mirror, retaining support for legacy symbol files."""
    root = tdir / "cn" / "daily"
    dated = sorted(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].parquet"))
    if dated:
        if any(root.glob("*.parquet")):
            raise ValueError(f"mixed per-date and per-symbol daily layout: {root}")
        lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        return [str(p) for p in dated if lo <= p.stem <= hi]
    return _existing([root / f"{s}.parquet" for s in symbols])


def ingest_cn_prices(
    tdir: Path, data_dir: Path, symbols: list[str], start: dt.date, end: dt.date
) -> dict:
    """``cn/daily`` raw OHLCV -> month-partitioned ``price_raw`` (market=CN)."""
    files = cn_daily_files(tdir, symbols, start, end)
    if not files:
        return {"rows": 0, "partitions": 0, "note": "no cn/daily files"}
    raw = pl.concat([
        pl.scan_parquet(p).select("trade_date", "ts_code", "open", "high", "low", "close", "vol")
        for p in files
    ], how="vertical_relaxed").filter(pl.col("ts_code").is_in(symbols)).collect()
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
    day_files = cn_daily_files(tdir, symbols, start, end)
    if not lim_files:
        return {"rows": 0, "note": "no cn/stk_limit files — run the cn_limit download"}
    if not day_files:
        return {"rows": 0, "note": "no cn/daily files in requested date range"}
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
        pl.concat([
            pl.scan_parquet(p).select("trade_date", "ts_code", "high", "low", "close")
            for p in day_files
        ], how="vertical_relaxed")
        .filter(pl.col("ts_code").is_in(symbols))
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
# HK — the raw mirror is sharded per trade date, not per symbol
# --------------------------------------------------------------------------- #
# A share count moving by more than this across a factor step is read as a real
# share change (split / consolidation / bonus issue) rather than the routine
# drift of option exercises and small placements.
_HK_SHARE_TOL = 0.02

# Tushare's HK feed carries two independent views of a corporate action: the step
# in ``cum_adjfactor``, and ``hk_daily.pre_close`` (which is restated onto the
# post-action basis). Each has artifacts of its own — the factor is occasionally
# re-anchored on a date with no action at all, and pre_close drifts off the prior
# close on thinly-traded names — so an event is emitted only where the two agree
# to within this relative tolerance. The factor supplies the magnitude; pre_close
# is the corroborating witness.
_HK_CORROBORATE_TOL = 0.01
# On a day the stock did not trade there is no pre_close, so the witness is the
# factor feed's own restated close. That number is only loosely consistent with
# the factor (the two feeds disagree by up to ~20% on real consolidations), which
# is far too coarse to tell a small dividend from a re-anchor. So halted-day steps
# are trusted only when the ratio is extreme enough to be unambiguously a share
# change — the case that actually matters, since consolidations are executed
# during a halt and leave a 40x gap if missed. Near-1 ratios on a halted day stay
# rejected: skipping a 2% dividend costs 2%, inventing one costs the same, and
# neither is worth the risk of readmitting a re-anchor.
_HK_HALTED_MIN_RATIO = 2.0
_HK_HALTED_CORROBORATE_TOL = 0.5


def hk_date_files(tdir: Path, subdir: str) -> list[str]:
    """Every year-sharded per-date parquet the HK downloader wrote for ``subdir``."""
    return sorted(str(p) for p in (tdir / "hk" / subdir).glob("*/*.parquet"))


def ingest_hk_prices(tdir: Path, data_dir: Path, start: dt.date, end: dt.date) -> dict:
    """``hk/daily`` raw OHLCV -> month-partitioned ``price_raw`` (market=HK)."""
    files = hk_date_files(tdir, "daily")
    if not files:
        return {"rows": 0, "partitions": 0, "note": "no hk/daily files"}
    px = (
        pl.scan_parquet(files)
        .select(
            _ymd("trade_date").alias("date"),
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            pl.col("open").cast(pl.Float32),
            pl.col("high").cast(pl.Float32),
            pl.col("low").cast(pl.Float32),
            pl.col("close").cast(pl.Float32),
            pl.col("vol").alias("volume").cast(pl.Float32),  # HK vol is in shares, not lots
            pl.lit(None).cast(pl.Int64).alias("transactions"),
        )
        .drop_nulls(["date", "symbol"])
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .with_columns(
            pl.col("date").alias("as_of_date"),
            pl.lit("tushare:hk_daily").alias("source_id"),
        )
        .select(list(PRICE_RAW_SCHEMA.keys()))
        .collect()
    )
    if px.is_empty():
        return {"rows": 0, "partitions": 0}
    rows = parts = 0
    for (year, month), part in px.with_columns(
        pl.col("date").dt.year().alias("_y"), pl.col("date").dt.month().alias("_m")
    ).group_by(["_y", "_m"]):
        part = part.drop(["_y", "_m"])
        path = price_partition_path(data_dir, HK_MARKET, int(year), int(month))
        upsert_parquet(path, part, keys=["date", "symbol"], sort_by=["date", "symbol"])
        rows += part.height
        parts += 1
    log.info("hk price_raw: %d rows across %d month partitions", rows, parts)
    return {"rows": rows, "partitions": parts}


def ingest_hk_adj_events(tdir: Path, data_dir: Path) -> dict:
    """``hk/adj_factor`` cumulative factor -> ``adj_events`` (market=HK).

    Each date where ``cum_adjfactor`` moves *and* ``pre_close`` corroborates is
    one event with total price ratio ``r = f / f_prev``; prices strictly before
    it scale by ``1 / r``. That ratio is attributed to either a share change or a
    cash distribution:

    * ``r < 1`` (the factor can only fall on a share increase) or a materially
      changed share count -> ``split_ratio = r``.
    * otherwise -> a cash distribution of ``close_prev * (1 - 1 / r)``, which is
      exactly the payout the engine's dividend rule turns back into ``1 / r``.

    Either branch reproduces ``r`` under ``adj="total"``, so the total-return
    series does not depend on the attribution being right.
    """
    files = hk_date_files(tdir, "adj_factor")
    if not files:
        return {"rows": 0, "note": "no hk/adj_factor files"}
    # The pre_close cross-check below is not optional: without it a vendor
    # re-anchor of the factor becomes a fabricated double-digit dividend, and
    # adj_events is upsert-only, so a bad event cannot be withdrawn by a later
    # run. Refuse to write anything rather than write something unwithdrawable.
    day_files = hk_date_files(tdir, "daily")
    if not day_files:
        return {"rows": 0, "note": "no hk/daily files — cannot corroborate factor steps"}
    fac = (
        pl.scan_parquet(files)
        .select(
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            _ymd("trade_date").alias("ex_date"),
            pl.col("cum_adjfactor").cast(pl.Float64).alias("f"),
            pl.col("close_price").cast(pl.Float64).alias("close"),
        )
        .drop_nulls(["symbol", "ex_date", "f"])
        .filter(pl.col("f") > 0)
        .unique(subset=["symbol", "ex_date"], keep="last")
        .sort(["symbol", "ex_date"])
        .with_columns(
            pl.col("f").shift(1).over("symbol").alias("f_prev"),
            pl.col("close").shift(1).over("symbol").alias("close_prev"),
        )
        .filter(pl.col("f_prev").is_not_null() & (pl.col("f") != pl.col("f_prev")))
        .with_columns((pl.col("f") / pl.col("f_prev")).alias("r"))
        .collect()
    )
    if fac.is_empty():
        return {"rows": 0}

    # Corroboration: on a real action the bar's pre_close is restated onto the
    # post-action basis, so close_prev / pre_close should reproduce r.
    witness = (
        pl.scan_parquet(day_files)
        .select(
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            _ymd("trade_date").alias("ex_date"),
            pl.col("pre_close").cast(pl.Float64),
        )
        .drop_nulls(["symbol", "ex_date"])
        .filter(pl.col("pre_close") > 0)
        .unique(subset=["symbol", "ex_date"], keep="last")
        .collect()
    )
    # A consolidation is normally executed while the stock is halted, so the very
    # events with the largest price ratios are the ones with no hk_daily bar to
    # witness them. Dropping those leaves the raw 40x gap in the panel — a far
    # worse error than the phantom dividends the check exists to prevent. The
    # factor feed carries its own close_price, which on a non-trading day is just
    # the restated price, so ``close_prev / close`` reproduces r there just as
    # ``close_prev / pre_close`` does on a trading day.
    n_raw = fac.height
    fac = fac.join(witness, on=["symbol", "ex_date"], how="left").with_columns(
        pl.when(pl.col("pre_close").is_not_null())
        .then(pl.col("close_prev") / pl.col("pre_close"))
        .otherwise(pl.col("close_prev") / pl.col("close"))
        .alias("r_witness"),
        pl.col("pre_close").is_not_null().alias("_traded"),
    )
    is_share_ratio = (pl.col("r") >= _HK_HALTED_MIN_RATIO) | (
        pl.col("r") <= 1.0 / _HK_HALTED_MIN_RATIO
    )
    agrees = (pl.col("r_witness") / pl.col("r") - 1.0).abs() <= pl.when(pl.col("_traded")).then(
        _HK_CORROBORATE_TOL
    ).otherwise(_HK_HALTED_CORROBORATE_TOL)
    fac = fac.filter(
        pl.col("r_witness").is_not_null()
        & agrees
        & (pl.col("_traded") | is_share_ratio)
    )
    n_halted = int((~fac["_traded"]).sum())
    log.info(
        "hk adj_events: %d/%d factor steps corroborated (%d of them on halted days)",
        fac.height, n_raw, n_halted,
    )
    if fac.is_empty():
        return {"rows": 0}

    share_files = hk_date_files(tdir, "daily_adj")
    if share_files:
        shares = (
            pl.scan_parquet(share_files)
            .select(
                pl.col("ts_code").alias("symbol").cast(pl.Utf8),
                _ymd("trade_date").alias("ex_date"),
                pl.col("total_share").cast(pl.Float64),
            )
            .drop_nulls(["symbol", "ex_date"])
            .unique(subset=["symbol", "ex_date"], keep="last")
            .sort(["symbol", "ex_date"])
            .with_columns(pl.col("total_share").shift(1).over("symbol").alias("share_prev"))
            .select("symbol", "ex_date", "total_share", "share_prev")
            .collect()
        )
        fac = fac.join(shares, on=["symbol", "ex_date"], how="left")
    else:
        fac = fac.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("total_share"),
            pl.lit(None, dtype=pl.Float64).alias("share_prev"),
        )

    share_known = (
        pl.col("share_prev").is_not_null()
        & (pl.col("share_prev") > 0)
        & pl.col("total_share").is_not_null()
    )
    share_moved = (
        share_known
        & ((pl.col("total_share") / pl.col("share_prev") - 1.0).abs() > _HK_SHARE_TOL)
    ).fill_null(False)
    # Attribution defaults to a share event, which is the safe way to be wrong. A
    # forward split misread as a dividend puts a fabricated cash payout worth half
    # the share price into the store, and ``adj="split"`` — the default read basis —
    # then shows the raw split gap as a real -50% return. The reverse error only
    # over-adjusts the split-only view by a dividend. So a step is called a cash
    # distribution *only* when the share count is known and demonstrably flat.
    is_share_event = (pl.col("r") < 1.0) | share_moved | ~share_known.fill_null(False)
    cash = pl.col("close_prev") * (1.0 - 1.0 / pl.col("r"))

    fac = fac.with_columns(is_share_event.alias("_share"), share_known.fill_null(False).alias("_known"))
    n_unknown = int((~fac["_known"]).sum())
    if n_unknown:
        log.warning(
            "hk adj_events: %d/%d steps have no share count (hk/daily_adj gap) — "
            "attributed to share events so adj='split' degrades safely",
            n_unknown, fac.height,
        )
    ev = (
        fac.select(
            pl.col("symbol"),
            pl.col("ex_date"),
            pl.col("ex_date").alias("as_of_date"),  # the factor step is knowable on its ex-date
            pl.when(pl.col("_share")).then(pl.lit("SPLIT")).otherwise(pl.lit("DIVIDEND")).alias("event_type"),
            pl.when(pl.col("_share")).then(pl.col("r")).otherwise(pl.lit(1.0)).alias("split_ratio"),
            pl.when(pl.col("_share")).then(pl.lit(0.0)).otherwise(cash).alias("dividend_cash"),
            pl.col("f").alias("provider_adj_factor"),
            (pl.lit("tushare:hkadj:") + pl.col("symbol") + ":" + pl.col("ex_date").cast(pl.Utf8)).alias("event_id"),
            pl.lit("tushare:hk_adjfactor").alias("source"),
        )
        # a cash branch with an unknown prior close carries no usable payout
        .filter((pl.col("split_ratio") != 1.0) | (pl.col("dividend_cash") > 0.0))
        .unique(subset=["event_id"], keep="last")
        .select(list(ADJ_EVENTS_SCHEMA.keys()))
    )
    if ev.is_empty():
        return {"rows": 0}
    path = adj_events_path(data_dir, HK_MARKET)
    n = upsert_parquet(path, ev, keys=["event_id"], sort_by=["symbol", "ex_date"])
    n_split = int((ev["event_type"] == "SPLIT").sum())
    log.info("hk adj_events: %d events (%d share, %d cash), %d total",
             ev.height, n_split, ev.height - n_split, n)
    return {"rows": ev.height, "share_events": n_split, "cash_events": ev.height - n_split, "total": n}


def hk_hshare_symbols(tdir: Path) -> list[str]:
    """HK tickers of **H-shares** — companies incorporated in mainland China.

    The discriminator is the ISIN country prefix, which is a property of where the
    issuer is registered and so never changes: ``CNE`` marks a mainland
    incorporation. Everything else on HKEX is a red chip (typically ``KYG``
    Cayman or ``BMG`` Bermuda) or a local Hong Kong issuer (``HK0``). Because the
    listings table is downloaded with the delisted set included, this list is
    survivorship-free.
    """
    path = tdir / "meta" / "hk_basic.parquet"
    if not path.is_file():
        return []
    basic = pl.read_parquet(path)
    if "isin" not in basic.columns:
        return []
    return sorted(
        basic.filter(pl.col("isin").cast(pl.Utf8).str.starts_with(_HK_MAINLAND_ISIN))
        .get_column("ts_code")
        .cast(pl.Utf8)
        .unique()
        .to_list()
    )


def _hk_liquidity_snapshots(
    tdir: Path,
    start: dt.date,
    end: dt.date,
    top_n: int,
    lookback_months: int,
    min_bars: int,
    restrict_to: list[str] | None = None,
) -> list[tuple[dt.date, list[str]]]:
    """Rank by trailing realised turnover; rebalance monthly, effective next month.

    Membership for month ``M+1`` is decided only from turnover through the end of
    month ``M`` and takes effect on ``M+1``'s first trading day, so no snapshot
    can see its own forward returns.

    ``restrict_to`` narrows the candidate pool before ranking (used to carve the
    H-share universe out of the whole market).
    """
    files = hk_date_files(tdir, "daily")
    if not files:
        return []
    bars = (
        pl.scan_parquet(files)
        .select(
            _ymd("trade_date").alias("date"),
            pl.col("ts_code").alias("symbol").cast(pl.Utf8),
            pl.col("amount").cast(pl.Float64),
        )
        .drop_nulls(["date", "symbol"])
        .pipe(lambda lf: lf if restrict_to is None else lf.filter(pl.col("symbol").is_in(restrict_to)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
        .group_by(["symbol", "month"])
        .agg(pl.col("amount").fill_null(0.0).sum().alias("turnover"), pl.len().alias("bars"))
        .collect()
    )
    if bars.is_empty():
        return []

    months = sorted(bars["month"].unique().to_list())
    # First trading day of each month — the effective date of the prior month's ranking.
    first_days = (
        pl.scan_parquet(files)
        .select(_ymd("trade_date").alias("date"))
        .drop_nulls(["date"])
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
        .group_by("month")
        .agg(pl.col("date").min().alias("first_day"))
        .collect()
    )
    first_by_month = dict(zip(first_days["month"], first_days["first_day"]))

    out: list[tuple[dt.date, list[str]]] = []
    for i, m in enumerate(months):
        if i + 1 >= len(months):
            break  # no following month to make the ranking effective in
        window = months[max(0, i + 1 - lookback_months) : i + 1]
        ranked = (
            bars.filter(pl.col("month").is_in(window))
            .group_by("symbol")
            .agg(pl.col("turnover").sum(), pl.col("bars").sum())
            # a name that barely traded over the window is not investable
            .filter((pl.col("turnover") > 0) & (pl.col("bars") >= min_bars))
            .sort("turnover", descending=True)
            .head(top_n)
        )
        members = sorted(ranked["symbol"].to_list())
        eff = first_by_month.get(months[i + 1])
        if members and eff is not None:
            out.append((eff, members))
    return out


def ingest_hk_universe(
    tdir: Path,
    data_dir: Path,
    start: dt.date,
    end: dt.date,
    *,
    top_n: int = 100,
    lookback_months: int = 3,
    min_bars: int = 20,
) -> dict:
    """Write the HK100 and HSHARE100 point-in-time universes plus HSI/HSTECH snapshots."""
    rows: list[dict] = []
    per_index: dict[str, int] = {}

    # Whole board, then the H-share slice of it. Both rank the same way, so the
    # two are directly comparable: HSHARE100 is HK100's methodology applied to
    # mainland-incorporated issuers only.
    hshares = hk_hshare_symbols(tdir)
    pools: list[tuple[str, list[str] | None]] = [(HK_LIQUID_INDEX, None)]
    if hshares:
        pools.append((HK_HSHARE_INDEX, hshares))
    else:
        log.warning(
            "hk universe: no ISIN data in meta/hk_basic.parquet — %s not built", HK_HSHARE_INDEX
        )
    for index_id, restrict in pools:
        snaps = _hk_liquidity_snapshots(
            tdir, start, end, top_n, lookback_months, min_bars, restrict_to=restrict
        )
        per_index[index_id] = len(snaps)
        for eff, members in snaps:
            rows.append(
                {"index_id": index_id, "effective_date": eff, "symbols": members, "as_of_date": eff}
            )

    cons_path = tdir / "hk" / "constituents.parquet"
    if cons_path.is_file():
        cons = pl.read_parquet(cons_path)
        for idx in HK_SNAPSHOT_INDICES:
            members = sorted(cons.filter(pl.col("index") == idx)["ts_code"].unique().to_list())
            if members:
                # A vendored *current* list with no history, so it is stamped at a
                # fixed epoch rather than the run's start date: keying it to the
                # window would mint a second, contradictory snapshot every time the
                # ingester ran over a different range, and upsert-on-key would keep
                # them all. Backdating it is survivorship-biased by nature — that is
                # the documented cost of these two universes.
                rows.append(
                    {
                        "index_id": idx,
                        "effective_date": _HK_SNAPSHOT_EPOCH,
                        "symbols": members,
                        "as_of_date": _HK_SNAPSHOT_EPOCH,
                    }
                )
                per_index[idx] = 1

    if not rows:
        return {"snapshots": 0}
    df = pl.DataFrame(rows, schema=UNIVERSE_SNAPSHOTS_SCHEMA)
    out = universe_snapshots_path(data_dir, HK_MARKET)
    n = upsert_parquet(
        out, df, keys=["index_id", "effective_date"], sort_by=["index_id", "effective_date"]
    )
    log.info("hk universe_snapshots: %s (%d total rows)", per_index, n)
    return {"snapshots": len(rows), "per_index": per_index, "total": n}


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


def prepare_hk(
    data_dir: str | Path,
    *,
    start: dt.date,
    end: dt.date,
    tushare_data_dir: str | Path | None = None,
    universe_start: dt.date | None = None,
    top_n: int = 100,
    min_bars: int = 20,
    do_universe: bool = True,
    do_prices: bool = True,
    do_adj: bool = True,
) -> dict:
    """Ingest the raw Tushare HK mirror into the canonical stores under ``data_dir``.

    Produces ``price_raw`` / ``adj_events`` / ``universe_snapshots`` for
    ``market=HK``, which a ``DataStore(AssayConfig(market="HK", data_dir=...))``
    reads directly. No symbol list is needed: the mirror is stored per trade date
    and the tradable universe is whatever actually traded.

    ``start``/``end`` bound the price window. ``universe_start`` (default: ``start``)
    bounds the membership ranking instead — an incremental run wants to append a
    few days of prices while still ranking over the full trailing history.
    """
    tdir = tushare_dir(tushare_data_dir)
    data_dir = Path(data_dir).expanduser()
    report: dict = {
        "market": HK_MARKET,
        "data_dir": str(data_dir),
        "range": [start.isoformat(), end.isoformat()],
    }
    log.info("prepare_hk: %s..%s -> %s", start, end, data_dir)
    if do_universe:
        report["universe"] = ingest_hk_universe(
            tdir, data_dir, universe_start or start, end, top_n=top_n, min_bars=min_bars
        )
    if do_prices:
        report["prices"] = ingest_hk_prices(tdir, data_dir, start, end)
    if do_adj:
        report["adj_events"] = ingest_hk_adj_events(tdir, data_dir)
    return report
