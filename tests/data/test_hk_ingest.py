"""Tushare raw -> canonical HK store ingest (multi-market data support).

Offline: a tiny synthetic HK mirror (per-*date* parquet, exactly the columns
``scripts/download_tushare.py --markets hk`` writes) is run through
:func:`assay.data.tushare.ingest.prepare_hk` and the canonical ``market=HK``
stores are checked.

The interesting behaviour is that HK has no dividend/split feed — only a
cumulative ``cum_adjfactor`` — so events are reconstructed from its steps,
corroborated against ``pre_close``, and attributed to a share change or a cash
payout via the share count. These tests pin that reconstruction, the
point-in-time HK100 membership ranking, and the round trip back through
:func:`assay.data.store.adjust.forward_adjust`.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from assay.data.schemas import (
    adj_events_path,
    price_partition_path,
    universe_snapshots_path,
)
from assay.data.store.adjust import forward_adjust
from assay.data.tushare.ingest import HK_HSHARE_INDEX, HK_LIQUID_INDEX, prepare_hk

DATES = ["20210104", "20210105", "20210106", "20210107"]


def _write(path, df: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def _mirror(src, *, closes, pre_closes, factors, shares, amounts):
    """Write a per-date HK mirror for the symbols keyed in ``closes``."""
    hk = src / "hk"
    for i, d in enumerate(DATES):
        syms = sorted(closes)
        _write(
            hk / "daily" / d[:4] / f"{d}.parquet",
            pl.DataFrame(
                {
                    "ts_code": syms,
                    "trade_date": [d] * len(syms),
                    "open": [closes[s][i] for s in syms],
                    "high": [closes[s][i] for s in syms],
                    "low": [closes[s][i] for s in syms],
                    "close": [closes[s][i] for s in syms],
                    "pre_close": [pre_closes[s][i] for s in syms],
                    "vol": [1000.0] * len(syms),
                    "amount": [amounts[s][i] for s in syms],
                }
            ),
        )
        _write(
            hk / "adj_factor" / d[:4] / f"{d}.parquet",
            pl.DataFrame(
                {
                    "ts_code": syms,
                    "trade_date": [d] * len(syms),
                    "cum_adjfactor": [factors[s][i] for s in syms],
                    "close_price": [closes[s][i] for s in syms],
                }
            ),
        )
        _write(
            hk / "daily_adj" / d[:4] / f"{d}.parquet",
            pl.DataFrame(
                {
                    "ts_code": syms,
                    "trade_date": [d] * len(syms),
                    "total_share": [shares[s][i] for s in syms],
                }
            ),
        )


def _flat(v, n=4):
    return [v] * n


def _basic_mirror(src):
    """AAA pays a cash dividend on day 3; BBB does a 1-for-2 consolidation."""
    closes = {"AAA.HK": [10.0, 10.0, 9.5, 9.5], "BBB.HK": [5.0, 5.0, 10.0, 10.0]}
    # pre_close is restated onto the post-action basis on the action day.
    pre_closes = {"AAA.HK": [10.0, 10.0, 9.5, 9.5], "BBB.HK": [5.0, 5.0, 10.0, 10.0]}
    # AAA: HK$0.50 cash on a HK$10 close -> r = 1/(1-0.05) = 1.0526...
    f_aaa = 1.0 / (1.0 - 0.5 / 10.0)
    factors = {"AAA.HK": [1.0, 1.0, f_aaa, f_aaa], "BBB.HK": [1.0, 1.0, 0.5, 0.5]}
    shares = {"AAA.HK": _flat(1_000.0), "BBB.HK": [1_000.0, 1_000.0, 500.0, 500.0]}
    amounts = {"AAA.HK": _flat(9e8), "BBB.HK": _flat(1e8)}
    _mirror(src, closes=closes, pre_closes=pre_closes, factors=factors, shares=shares, amounts=amounts)


def _run(tmp_path, **kw):
    src, out = tmp_path / "raw", tmp_path / "assay"
    _basic_mirror(src)
    rep = prepare_hk(
        out, start=dt.date(2021, 1, 4), end=dt.date(2021, 1, 7), tushare_data_dir=src, **kw
    )
    return src, out, rep


def test_prices_land_in_the_hk_partition(tmp_path):
    _, out, rep = _run(tmp_path)
    assert rep["prices"]["rows"] == 8
    px = pl.read_parquet(price_partition_path(out, "HK", 2021, 1))
    assert set(px["symbol"].unique()) == {"AAA.HK", "BBB.HK"}
    assert px["source_id"].unique().to_list() == ["tushare:hk_daily"]
    # HK volume is in shares, carried through unscaled.
    assert px.filter(pl.col("symbol") == "AAA.HK")["volume"].to_list() == [1000.0] * 4


def test_cash_and_share_events_are_told_apart(tmp_path):
    _, out, rep = _run(tmp_path)
    ev = pl.read_parquet(adj_events_path(out, "HK")).sort("symbol")
    assert rep["adj_events"]["rows"] == 2
    assert dict(zip(ev["symbol"], ev["event_type"])) == {
        "AAA.HK": "DIVIDEND",  # share count unchanged, factor rose
        "BBB.HK": "SPLIT",     # share count halved, factor fell
    }
    aaa = ev.filter(pl.col("symbol") == "AAA.HK").row(0, named=True)
    assert aaa["split_ratio"] == 1.0
    assert abs(aaa["dividend_cash"] - 0.5) < 1e-9  # recovered from the factor step
    bbb = ev.filter(pl.col("symbol") == "BBB.HK").row(0, named=True)
    assert abs(bbb["split_ratio"] - 0.5) < 1e-9
    assert bbb["dividend_cash"] == 0.0
    assert ev["ex_date"].to_list() == [dt.date(2021, 1, 6)] * 2


def test_total_return_adjustment_round_trips(tmp_path):
    """Replaying the events must reproduce the vendor's own price ratios."""
    src, out, _ = _run(tmp_path)
    px = pl.read_parquet(price_partition_path(out, "HK", 2021, 1)).select(
        "date", "symbol", pl.col("close").cast(pl.Float64), pl.col("volume").cast(pl.Float64)
    )
    ev = pl.read_parquet(adj_events_path(out, "HK"))
    adj = forward_adjust(px, ev, mode="total").sort(["symbol", "date"])

    # The consolidation day is a pure share change: the adjusted series must be
    # continuous across it (5.0 -> 10.0 raw becomes 10.0 -> 10.0 adjusted).
    bbb = adj.filter(pl.col("symbol") == "BBB.HK")["close"].to_list()
    assert [round(c, 6) for c in bbb] == [10.0, 10.0, 10.0, 10.0]
    # ...and volume is restated into post-consolidation share units: 1000 old
    # shares is 500 new ones, so notional traded stays comparable.
    assert adj.filter(pl.col("symbol") == "BBB.HK")["volume"].to_list()[0] == 500.0

    # The dividend day: pre-ex closes scale by 1 - 0.5/10 = 0.95.
    aaa = adj.filter(pl.col("symbol") == "AAA.HK")["close"].to_list()
    assert [round(c, 6) for c in aaa] == [9.5, 9.5, 9.5, 9.5]


def test_split_mode_leaves_the_dividend_alone(tmp_path):
    _, out, _ = _run(tmp_path)
    px = pl.read_parquet(price_partition_path(out, "HK", 2021, 1)).select(
        "date", "symbol", pl.col("close").cast(pl.Float64)
    )
    ev = pl.read_parquet(adj_events_path(out, "HK"))
    adj = forward_adjust(px, ev, mode="split").sort(["symbol", "date"])
    # split-only: AAA is untouched (raw), BBB is still de-consolidated.
    assert adj.filter(pl.col("symbol") == "AAA.HK")["close"].to_list() == [10.0, 10.0, 9.5, 9.5]
    assert [round(c, 6) for c in adj.filter(pl.col("symbol") == "BBB.HK")["close"].to_list()] == [
        10.0, 10.0, 10.0, 10.0
    ]


def test_factor_step_without_pre_close_support_is_dropped(tmp_path):
    """A re-anchored factor on a day the price did not actually move is not an event."""
    src, out = tmp_path / "raw", tmp_path / "assay"
    closes = {"AAA.HK": [10.0, 10.0, 10.0, 10.0]}
    pre_closes = {"AAA.HK": [10.0, 10.0, 10.0, 10.0]}  # no restatement -> no action
    factors = {"AAA.HK": [0.8, 0.8, 1.0, 1.0]}         # vendor re-anchor
    _mirror(
        src, closes=closes, pre_closes=pre_closes, factors=factors,
        shares={"AAA.HK": _flat(1_000.0)}, amounts={"AAA.HK": _flat(1e8)},
    )
    rep = prepare_hk(
        out, start=dt.date(2021, 1, 4), end=dt.date(2021, 1, 7), tushare_data_dir=src
    )
    assert rep["adj_events"]["rows"] == 0
    assert not adj_events_path(out, "HK").exists()


def test_split_is_not_mistaken_for_a_dividend_without_share_data(tmp_path):
    """A missing hk/daily_adj must degrade the split view, never fabricate a crash.

    Attributing a 2-for-1 split to a cash payout would put half the share price
    into ``dividend_cash``; ``adj="split"`` (the default read basis) would then
    leave the raw price gap in place and every factor would read a -50% return.
    """
    src, out = tmp_path / "raw", tmp_path / "assay"
    closes = {"AAA.HK": [20.0, 20.0, 10.0, 10.0]}
    pre_closes = {"AAA.HK": [20.0, 20.0, 10.0, 10.0]}
    factors = {"AAA.HK": [1.0, 1.0, 2.0, 2.0]}  # forward split -> factor doubles
    _mirror(
        src, closes=closes, pre_closes=pre_closes, factors=factors,
        shares={"AAA.HK": _flat(1_000.0)}, amounts={"AAA.HK": _flat(1e8)},
    )
    # drop the share-count mirror entirely
    import shutil
    shutil.rmtree(src / "hk" / "daily_adj")

    prepare_hk(out, start=dt.date(2021, 1, 4), end=dt.date(2021, 1, 7), tushare_data_dir=src)
    ev = pl.read_parquet(adj_events_path(out, "HK"))
    assert ev["event_type"].to_list() == ["SPLIT"]
    assert ev["dividend_cash"].to_list() == [0.0]

    px = pl.read_parquet(price_partition_path(out, "HK", 2021, 1)).select(
        "date", "symbol", pl.col("close").cast(pl.Float64)
    )
    for mode in ("split", "total"):
        adj = forward_adjust(px, ev, mode=mode).sort("date")
        assert [round(c, 6) for c in adj["close"].to_list()] == [10.0, 10.0, 10.0, 10.0], mode


def test_no_events_without_prices_to_corroborate_against(tmp_path):
    """adj_events is upsert-only, so an uncorroborated guess could never be withdrawn."""
    src, out = tmp_path / "raw", tmp_path / "assay"
    closes = {"AAA.HK": [10.0, 10.0, 10.0, 10.0]}
    factors = {"AAA.HK": [0.8, 0.8, 1.0, 1.0]}  # vendor re-anchor, price flat
    _mirror(
        src, closes=closes, pre_closes=closes, factors=factors,
        shares={"AAA.HK": _flat(1_000.0)}, amounts={"AAA.HK": _flat(1e8)},
    )
    import shutil
    shutil.rmtree(src / "hk" / "daily")

    rep = prepare_hk(out, start=dt.date(2021, 1, 4), end=dt.date(2021, 1, 7), tushare_data_dir=src)
    assert rep["adj_events"]["rows"] == 0
    assert "corroborate" in rep["adj_events"]["note"]
    assert not adj_events_path(out, "HK").exists()


def test_h_shares_are_carved_out_by_isin(tmp_path):
    """HSHARE100 is HK100's ranking restricted to mainland-incorporated issuers."""
    src, out = tmp_path / "raw", tmp_path / "assay"
    hk = src / "hk"
    # AAA/BBB are H-shares (CNE ISIN), CCC is a Cayman red chip, DDD a local issuer.
    # CCC out-trades everything, so a universe that ignored ISIN would include it.
    turnover = {"AAA.HK": 5e8, "BBB.HK": 4e8, "CCC.HK": 9e9, "DDD.HK": 8e9}
    syms = sorted(turnover)
    for d in ["20210104", "20210105", "20210201", "20210202"]:
        _write(
            hk / "daily" / d[:4] / f"{d}.parquet",
            pl.DataFrame(
                {
                    "ts_code": syms, "trade_date": [d] * 4,
                    "open": [10.0] * 4, "high": [10.0] * 4, "low": [10.0] * 4,
                    "close": [10.0] * 4, "pre_close": [10.0] * 4, "vol": [1000.0] * 4,
                    "amount": [turnover[s] for s in syms],
                }
            ),
        )
    _write(
        src / "meta" / "hk_basic.parquet",
        pl.DataFrame(
            {
                "ts_code": syms,
                "isin": ["CNE100000AA1", "CNE100000BB2", "KYG100000CC3", "HK0000000DD4"],
            }
        ),
    )
    prepare_hk(
        out, start=dt.date(2021, 1, 1), end=dt.date(2021, 2, 28),
        tushare_data_dir=src, top_n=10, min_bars=1, do_adj=False,
    )
    snaps = pl.read_parquet(universe_snapshots_path(out, "HK"))
    hshare = snaps.filter(pl.col("index_id") == HK_HSHARE_INDEX)
    assert hshare.height == 1
    assert hshare["symbols"].to_list() == [["AAA.HK", "BBB.HK"]]
    # the whole-board universe still admits the red chip and the local issuer
    assert set(snaps.filter(pl.col("index_id") == HK_LIQUID_INDEX)["symbols"][0]) == set(syms)


def test_no_hshare_universe_without_isin_data(tmp_path):
    """Without ISINs the H-share carve-out is unknowable — omit it, don't guess."""
    src, out = tmp_path / "raw", tmp_path / "assay"
    _basic_mirror(src)  # writes no meta/hk_basic.parquet
    rep = prepare_hk(
        out, start=dt.date(2021, 1, 4), end=dt.date(2021, 1, 7),
        tushare_data_dir=src, min_bars=1, do_adj=False,
    )
    assert HK_HSHARE_INDEX not in rep["universe"].get("per_index", {})


def test_snapshot_universes_do_not_multiply_across_runs(tmp_path):
    """HSI/HSTECH carry no history, so their snapshot must not follow the run window.

    Keying them to the run's start date minted a fresh, contradictory snapshot on
    every ingest over a different range, and the upsert key kept them all.
    """
    src, out = tmp_path / "raw", tmp_path / "assay"
    _basic_mirror(src)
    _write(
        src / "hk" / "constituents.parquet",
        pl.DataFrame({"ts_code": ["AAA.HK", "BBB.HK"], "index": ["HSI", "HSTECH"]}),
    )
    for start in (dt.date(2021, 1, 4), dt.date(2015, 1, 1), dt.date(2018, 6, 1)):
        prepare_hk(
            out, start=start, end=dt.date(2021, 1, 7), tushare_data_dir=src, do_adj=False
        )
    snaps = pl.read_parquet(universe_snapshots_path(out, "HK"))
    counts = dict(snaps.group_by("index_id").len().iter_rows())
    assert counts["HSI"] == 1 and counts["HSTECH"] == 1


def test_hk100_membership_is_point_in_time(tmp_path):
    """A month's ranking is built from that month and takes effect the next one."""
    src, out = tmp_path / "raw", tmp_path / "assay"
    hk = src / "hk"
    # Two months. In January AAA out-trades BBB; in February they swap.
    months = {
        "2021-01": ["20210104", "20210105"],
        "2021-02": ["20210201", "20210202"],
    }
    turnover = {"2021-01": {"AAA.HK": 9e8, "BBB.HK": 1e8}, "2021-02": {"AAA.HK": 1e8, "BBB.HK": 9e8}}
    for mkey, days in months.items():
        for d in days:
            syms = ["AAA.HK", "BBB.HK"]
            _write(
                hk / "daily" / d[:4] / f"{d}.parquet",
                pl.DataFrame(
                    {
                        "ts_code": syms, "trade_date": [d] * 2,
                        "open": [10.0, 10.0], "high": [10.0, 10.0], "low": [10.0, 10.0],
                        "close": [10.0, 10.0], "pre_close": [10.0, 10.0],
                        "vol": [1000.0, 1000.0],
                        "amount": [turnover[mkey][s] for s in syms],
                    }
                ),
            )
    prepare_hk(
        out, start=dt.date(2021, 1, 1), end=dt.date(2021, 2, 28),
        tushare_data_dir=src, top_n=1, min_bars=1, do_adj=False,
    )
    snaps = (
        pl.read_parquet(universe_snapshots_path(out, "HK"))
        .filter(pl.col("index_id") == HK_LIQUID_INDEX)
        .sort("effective_date")
    )
    # January's ranking (AAA ahead) becomes effective on February's first bar —
    # never on a date inside the window that produced it.
    assert snaps["effective_date"].to_list() == [dt.date(2021, 2, 1)]
    assert snaps["symbols"].to_list() == [["AAA.HK"]]
