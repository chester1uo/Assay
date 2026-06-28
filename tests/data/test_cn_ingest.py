"""Tushare raw -> canonical CN store ingest (multi-market data support).

Offline: a tiny synthetic Tushare mirror (per-symbol parquet, exactly the columns
``scripts/download_tushare.py`` writes) is run through
:func:`assay.data.tushare.ingest.prepare_cn` and the canonical ``market=CN``
stores are checked. Verifies the non-obvious mappings: 送转 -> forward
``split_ratio``, pre-tax cash -> ``dividend_cash``, only *implemented* dividends
become events, ``index_weight`` -> de-duplicated membership snapshots, and the
``stk_limit`` + daily join -> limit bands with locked flags.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from assay.data.schemas import (
    adj_events_path,
    price_partition_path,
    security_groups_path,
    trade_status_path,
    universe_snapshots_path,
)
from assay.data.tushare.ingest import prepare_cn


def _write(path, df: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def _build_raw_mirror(src):
    """Two symbols (AAA, BBB) of synthetic Tushare raw parquet under ``src``."""
    cn = src / "cn"
    # --- daily (raw OHLCV); AAA has a 转增 mid-series, BBB plain --------------
    days = ["20210104", "20210105", "20210106", "20210107"]
    for sym, base in (("AAA.SZ", 10.0), ("BBB.SZ", 20.0)):
        _write(
            cn / "daily" / f"{sym}.parquet",
            pl.DataFrame(
                {
                    "ts_code": [sym] * 4,
                    "trade_date": days,
                    "open": [base, base + 1, base + 2, base + 3],
                    "high": [base + 0.5, base + 1.5, base + 2.5, base + 3.5],
                    "low": [base - 0.5, base + 0.5, base + 1.5, base + 2.5],
                    "close": [base, base + 1, base + 2, base + 3],
                    "vol": [1000.0, 1100.0, 1200.0, 1300.0],
                }
            ),
        )
    # --- dividend: AAA implemented 10转5 (stk_div 0.5) + cash 0.30 pre-tax;
    #     plus a *proposed* (预案) row that must be ignored ---------------------
    _write(
        cn / "dividend" / "AAA.SZ.parquet",
        pl.DataFrame(
            {
                "ts_code": ["AAA.SZ", "AAA.SZ"],
                "end_date": ["20201231", "20201231"],
                "ann_date": ["20210101", "20210101"],
                "imp_ann_date": ["20210103", None],
                "div_proc": ["实施", "预案"],
                "stk_div": [0.5, 0.0],
                "stk_bo_rate": [0.0, 0.0],
                "stk_co_rate": [0.5, 0.0],
                "cash_div": [0.27, 0.0],
                "cash_div_tax": [0.30, 0.0],
                "ex_date": ["20210106", None],
            }
        ),
    )
    _write(  # BBB: no dividends (empty frame with the right columns)
        cn / "dividend" / "BBB.SZ.parquet",
        pl.DataFrame(
            schema={
                "ts_code": pl.Utf8, "end_date": pl.Utf8, "ann_date": pl.Utf8,
                "imp_ann_date": pl.Utf8, "div_proc": pl.Utf8, "stk_div": pl.Float64,
                "stk_bo_rate": pl.Float64, "stk_co_rate": pl.Float64,
                "cash_div": pl.Float64, "cash_div_tax": pl.Float64, "ex_date": pl.Utf8,
            }
        ),
    )
    # --- index_weight (CSI300): {AAA,BBB} on day1, then BBB leaves on day3 so the
    #     *membership set* genuinely changes (a weight-only change would collapse) -
    _write(
        cn / "index_weight" / "CSI300.parquet",
        pl.DataFrame(
            {
                "index_code": ["000300.SH"] * 3,
                "con_code": ["AAA.SZ", "BBB.SZ", "AAA.SZ"],
                "trade_date": ["20210104", "20210104", "20210106"],
                "weight": [60.0, 40.0, 100.0],
            }
        ),
    )
    # --- stk_limit: AAA locked limit-up on day 2 (low >= up_limit) ------------
    _write(
        cn / "stk_limit" / "AAA.SZ.parquet",
        pl.DataFrame(
            {
                "trade_date": days,
                "ts_code": ["AAA.SZ"] * 4,
                "up_limit": [11.0, 10.5, 13.0, 14.0],  # day2 up_limit 10.5 <= low 10.5 -> locked
                "down_limit": [9.0, 9.5, 10.0, 11.0],
            }
        ),
    )
    _write(
        cn / "stk_limit" / "BBB.SZ.parquet",
        pl.DataFrame(
            {
                "trade_date": days,
                "ts_code": ["BBB.SZ"] * 4,
                "up_limit": [22.0, 23.0, 24.0, 25.0],
                "down_limit": [18.0, 19.0, 20.0, 21.0],
            }
        ),
    )
    # --- universe.parquet (symbol union the downloader writes) ----------------
    _write(cn / "universe.parquet", pl.DataFrame({"ts_code": ["AAA.SZ", "BBB.SZ"]}))
    # --- meta/stock_basic (industry -> security_groups) -----------------------
    _write(
        src / "meta" / "stock_basic.parquet",
        pl.DataFrame(
            {
                "ts_code": ["AAA.SZ", "BBB.SZ"],
                "symbol": ["AAA", "BBB"],
                "name": ["甲", "乙"],
                "area": ["深圳", "上海"],
                "industry": ["银行", "白酒"],
                "fullname": ["甲", "乙"],
                "market": ["主板", "主板"],
                "exchange": ["SZSE", "SZSE"],
                "list_status": ["L", "L"],
                "list_date": ["20100104", "20100104"],
                "delist_date": [None, None],
                "is_hs": ["N", "N"],
            }
        ),
    )


def test_prepare_cn_maps_all_stores(tmp_path):
    src = tmp_path / "tushare"
    out = tmp_path / "canonical"
    _build_raw_mirror(src)

    rep = prepare_cn(out, start=dt.date(2021, 1, 1), end=dt.date(2021, 12, 31), tushare_data_dir=src)
    assert rep["prices"]["rows"] == 8  # 2 symbols x 4 days
    assert rep["n_symbols"] == 2

    # price_raw: raw OHLCV, market=CN, Jan-2021 partition
    px = pl.read_parquet(str(price_partition_path(out, "CN", 2021, 1)))
    assert set(px.columns) >= {"date", "symbol", "open", "close", "volume", "as_of_date", "source_id"}
    assert px.filter(pl.col("symbol") == "AAA.SZ").height == 4
    assert px["source_id"][0] == "tushare:daily"

    # adj_events: exactly ONE event for AAA (implemented), with the 转增 split
    # ratio 1.5 AND the pre-tax cash 0.30; the 预案 row is dropped.
    ae = pl.read_parquet(str(adj_events_path(out, "CN")))
    aaa = ae.filter(pl.col("symbol") == "AAA.SZ")
    assert aaa.height == 1
    row = aaa.row(0, named=True)
    assert abs(row["split_ratio"] - 1.5) < 1e-9      # 1 + stk_div(0.5)
    assert abs(row["dividend_cash"] - 0.30) < 1e-9   # cash_div_tax (pre-tax)
    assert row["ex_date"] == dt.date(2021, 1, 6)
    assert row["as_of_date"] == dt.date(2021, 1, 3)  # imp_ann_date
    assert ae.filter(pl.col("symbol") == "BBB.SZ").height == 0

    # universe_snapshots: CSI300, two composition snapshots (day1 + day3)
    us = pl.read_parquet(str(universe_snapshots_path(out, "CN")))
    csi = us.filter(pl.col("index_id") == "CSI300").sort("effective_date")
    assert csi.height == 2
    assert csi["effective_date"].to_list() == [dt.date(2021, 1, 4), dt.date(2021, 1, 6)]
    assert sorted(csi["symbols"][0]) == ["AAA.SZ", "BBB.SZ"]
    assert sorted(csi["symbols"][1]) == ["AAA.SZ"]  # BBB left on day 3

    # trade_status: bands present; AAA locked limit-up on day 2 only
    ts = pl.read_parquet(str(trade_status_path(out, "CN")))
    aaa_ts = ts.filter(pl.col("symbol") == "AAA.SZ").sort("date")
    assert aaa_ts.height == 4
    locked = aaa_ts.filter(pl.col("limit_up_locked"))["date"].to_list()
    assert locked == [dt.date(2021, 1, 5)]  # day 2: low 10.5 >= up_limit 10.5
    assert not ts.filter(pl.col("symbol") == "BBB.SZ")["limit_up_locked"].any()

    # security_groups: industry label per symbol (from stock_basic)
    sg = pl.read_parquet(str(security_groups_path(out, "CN")))
    assert dict(zip(sg["symbol"].to_list(), sg["group"].to_list())) == {
        "AAA.SZ": "银行",
        "BBB.SZ": "白酒",
    }
