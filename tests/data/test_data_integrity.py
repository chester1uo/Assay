"""Data-integrity checks: corporate actions must not leave abnormal jumps.

Two layers:

* **Property tests on ``forward_adjust``** — a smooth underlying price with a
  split / reverse-split / dividend embedded should, after adjustment, have *no*
  abnormal day-over-day jump, even though the *raw* series does. Volume must be
  continuous across a split too.
* **End-to-end through ``DataStore.get_panel``** — writes temporary parquet
  stores and verifies the adjusted panel is jump-free AND that a corporate action
  is only applied once it is *knowable* (point-in-time correctness): the same
  date range adjusted ``as_of`` a date before the split's ``knowledge_time``
  still shows the raw jump.

Run with::

    PYTHONPATH=src python -m pytest tests/data/test_data_integrity.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.config import AssayConfig, MassiveConfig
from assay.data.schemas import (
    adj_events_path,
    price_partition_path,
    universe_snapshots_path,
)
from assay.data.store import DataStore
from assay.data.store.adjust import forward_adjust


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _prices(rows):
    return pl.DataFrame(
        rows,
        schema={"date": pl.Date, "symbol": pl.Utf8, "close": pl.Float64, "volume": pl.Float64},
    )


def _events(rows):
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.Utf8,
            "ex_date": pl.Date,
            "split_ratio": pl.Float64,
            "dividend_cash": pl.Float64,
        },
    )


def max_log_jump(values) -> float:
    """Largest absolute day-over-day log change of a price series."""
    c = np.asarray(values, dtype=float)
    return float(np.max(np.abs(np.diff(np.log(c)))))


DAYS = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(40)]
DRIFT = 1.002  # smooth ~0.2%/day underlying; a split (~69% log) dwarfs it


# ---------------------------------------------------------------------------
# forward_adjust property tests
# ---------------------------------------------------------------------------


def test_split_leaves_no_abnormal_jump_and_recovers_true_price():
    ex = 20
    true = np.array([100.0 * DRIFT**i for i in range(40)])  # post-split basis
    raw = true.copy()
    raw[:ex] *= 2.0  # before a 2:1 split the price was doubled (fewer shares)
    prices = _prices(
        [{"date": DAYS[i], "symbol": "AAA", "close": raw[i], "volume": 1000.0} for i in range(40)]
    )
    events = _events([{"symbol": "AAA", "ex_date": DAYS[ex], "split_ratio": 2.0, "dividend_cash": 0.0}])

    adj = forward_adjust(prices, events, mode="split").sort("date")["close"].to_numpy()

    # raw has a ~50% (ln2 ≈ 0.69) jump at the ex-date; adjusted does not
    assert max_log_jump(raw) > 0.5
    assert max_log_jump(adj) < 0.01
    # the adjusted series recovers the smooth true (post-split-basis) price
    np.testing.assert_allclose(adj, true, rtol=1e-9)


def test_reverse_split_leaves_no_abnormal_jump():
    ex = 18
    true = np.array([50.0 * DRIFT**i for i in range(40)])
    raw = true.copy()
    raw[:ex] /= 10.0  # before a 1:10 reverse split the price was 10x lower
    prices = _prices(
        [{"date": DAYS[i], "symbol": "ZZZ", "close": raw[i], "volume": 1000.0} for i in range(40)]
    )
    events = _events([{"symbol": "ZZZ", "ex_date": DAYS[ex], "split_ratio": 0.1, "dividend_cash": 0.0}])

    adj = forward_adjust(prices, events, mode="split").sort("date")["close"].to_numpy()
    assert max_log_jump(raw) > 2.0  # ln(10) ≈ 2.3 jump in raw
    assert max_log_jump(adj) < 0.01
    np.testing.assert_allclose(adj, true, rtol=1e-9)


def test_volume_is_continuous_across_split():
    ex = 20
    # share volume roughly doubles after a 2:1 split (same dollar turnover)
    vol = np.where(np.arange(40) < ex, 1000.0, 2000.0)
    prices = _prices(
        [{"date": DAYS[i], "symbol": "AAA", "close": 100.0, "volume": float(vol[i])} for i in range(40)]
    )
    events = _events([{"symbol": "AAA", "ex_date": DAYS[ex], "split_ratio": 2.0, "dividend_cash": 0.0}])
    adj_vol = forward_adjust(prices, events, mode="split").sort("date")["volume"].to_numpy()
    # raw volume doubles at the split; adjusted volume is flat (pre-split scaled up)
    assert max_log_jump(vol) > 0.5
    assert max_log_jump(adj_vol) < 1e-9
    np.testing.assert_allclose(adj_vol, 2000.0, rtol=1e-9)


def test_dividend_total_return_removes_the_ex_date_drop():
    # flat price 100 that drops to 99 purely because of a $1 dividend on the ex-date
    closes = [100.0] * 5 + [99.0] * 5
    days = DAYS[:10]
    prices = _prices([{"date": days[i], "symbol": "AAA", "close": closes[i], "volume": 10.0} for i in range(10)])
    events = _events([{"symbol": "AAA", "ex_date": days[5], "split_ratio": 1.0, "dividend_cash": 1.0}])

    raw = np.array(closes)
    total = forward_adjust(prices, events, mode="total").sort("date")["close"].to_numpy()
    # raw drops 1% on the ex-date; the total-return series is flat (dividend removed)
    assert max_log_jump(raw) == pytest.approx(abs(np.log(99 / 100)), rel=1e-9)
    assert max_log_jump(total) < 1e-9
    np.testing.assert_allclose(total, 99.0, rtol=1e-9)
    # split mode leaves the dividend drop in place
    split = forward_adjust(prices, events, mode="split").sort("date")["close"].to_numpy()
    assert max_log_jump(split) > 0.009


def test_combined_split_and_dividend_continuous():
    # flat 100; 2:1 split at day 10; $1 dividend at day 20 (raw close just before is 50)
    closes = [100.0] * 10 + [50.0] * 10 + [49.0] * 10
    days = DAYS[:30]
    prices = _prices([{"date": days[i], "symbol": "AAA", "close": closes[i], "volume": 10.0} for i in range(30)])
    events = _events([
        {"symbol": "AAA", "ex_date": days[10], "split_ratio": 2.0, "dividend_cash": 0.0},
        {"symbol": "AAA", "ex_date": days[20], "split_ratio": 1.0, "dividend_cash": 1.0},
    ])
    total = forward_adjust(prices, events, mode="total").sort("date")["close"].to_numpy()
    assert max_log_jump(np.array(closes)) > 0.6   # raw has the split jump
    assert max_log_jump(total) < 1e-9             # fully continuous after adjustment
    np.testing.assert_allclose(total, 49.0, rtol=1e-9)


# ---------------------------------------------------------------------------
# DataStore end-to-end: adjustment + point-in-time correctness
# ---------------------------------------------------------------------------


def _make_store(tmp_path) -> AssayConfig:
    cfg = AssayConfig(
        massive=MassiveConfig(api_key="x", s3_access_key_id="x", s3_secret_access_key="x"),
        data_dir=tmp_path,
        market="US",
    )
    days = [dt.date(2024, 1, d) for d in range(2, 27)]  # all January -> one partition
    split_ex = dt.date(2024, 1, 15)
    # raw close: doubled before the split, flat 100 after
    closes = [200.0 if d < split_ex else 100.0 for d in days]
    price = pl.DataFrame(
        {
            "date": days,
            "symbol": ["AAA"] * len(days),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0e6] * len(days),
            "transactions": [100] * len(days),
            "as_of_date": days,  # EOD bar knowable same day
            "source_id": ["test"] * len(days),
        }
    ).with_columns(
        pl.col(["open", "high", "low", "close", "volume"]).cast(pl.Float32),
        pl.col("transactions").cast(pl.Int64),
    )
    ppath = price_partition_path(tmp_path, "US", 2024, 1)
    ppath.parent.mkdir(parents=True, exist_ok=True)
    price.write_parquet(ppath)

    # split known only as-of 2024-01-31 (a late-known action) -> tests PIT
    events = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "ex_date": [split_ex],
            "as_of_date": [dt.date(2024, 1, 31)],
            "event_type": ["SPLIT"],
            "split_ratio": [2.0],
            "dividend_cash": [0.0],
            "provider_adj_factor": [0.5],
            "event_id": ["e1"],
            "source": ["test"],
        }
    )
    epath = adj_events_path(tmp_path, "US")
    epath.parent.mkdir(parents=True, exist_ok=True)
    events.write_parquet(epath)

    # universe snapshots for the PIT membership check
    snaps = pl.DataFrame(
        {
            "index_id": ["NASDAQ100", "NASDAQ100"],
            "effective_date": [dt.date(2024, 1, 1), dt.date(2024, 1, 10)],
            "symbols": [["AAA"], ["AAA", "BBB"]],
            "as_of_date": [dt.date(2024, 1, 1), dt.date(2024, 1, 10)],
        }
    )
    upath = universe_snapshots_path(tmp_path, "US")
    upath.parent.mkdir(parents=True, exist_ok=True)
    snaps.write_parquet(upath)
    return cfg


def test_datastore_split_adjusted_panel_has_no_jump(tmp_path):
    store = DataStore(_make_store(tmp_path))
    panel = store.get_panel(
        fields=["close"],
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-26",
        as_of_date="2024-01-31",  # split is knowable
        adj="split",
    ).sort("date")
    close = panel["close"].to_numpy()
    assert max_log_jump(close) < 1e-6           # split removed -> flat 100
    np.testing.assert_allclose(close, 100.0, rtol=1e-5)


def test_datastore_pit_split_not_applied_before_known(tmp_path):
    store = DataStore(_make_store(tmp_path))
    # same range, but as-of BEFORE the split's knowledge_time -> not yet applied
    panel = store.get_panel(
        fields=["close"],
        symbols=["AAA"],
        start_date="2024-01-02",
        end_date="2024-01-26",
        as_of_date="2024-01-20",
        adj="split",
    ).sort("date")
    close = panel["close"].to_numpy()
    assert max_log_jump(close) > 0.5            # raw 200 -> 100 jump still present
    assert close.min() == pytest.approx(100.0, rel=1e-5)
    assert close.max() == pytest.approx(200.0, rel=1e-5)


def test_datastore_adj_none_is_raw(tmp_path):
    store = DataStore(_make_store(tmp_path))
    panel = store.get_panel(
        fields=["close"], symbols=["AAA"],
        start_date="2024-01-02", end_date="2024-01-26",
        as_of_date="2024-01-31", adj="none",
    ).sort("date")
    assert max_log_jump(panel["close"].to_numpy()) > 0.5  # unadjusted keeps the jump


def test_datastore_universe_is_point_in_time(tmp_path):
    store = DataStore(_make_store(tmp_path))
    # only the first snapshot is knowable on 2024-01-05
    early = store.get_universe("NASDAQ100", date="2024-01-20", as_of_date="2024-01-05")
    assert early == ["AAA"]
    # by 2024-01-15 the second snapshot is knowable
    later = store.get_universe("NASDAQ100", date="2024-01-20", as_of_date="2024-01-15")
    assert sorted(later) == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# review-driven: look-ahead (ex_date > end) guard, total-return path, multi-symbol, errors
# ---------------------------------------------------------------------------

_PRICE_SCHEMA = {
    "date": pl.Date, "symbol": pl.Utf8, "open": pl.Float64, "high": pl.Float64,
    "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
    "transactions": pl.Int64, "as_of_date": pl.Date, "source_id": pl.Utf8,
}
_EVENT_SCHEMA = {
    "symbol": pl.Utf8, "ex_date": pl.Date, "as_of_date": pl.Date, "event_type": pl.Utf8,
    "split_ratio": pl.Float64, "dividend_cash": pl.Float64, "provider_adj_factor": pl.Float64,
    "event_id": pl.Utf8, "source": pl.Utf8,
}


def _build_store(tmp_path, price_rows, event_rows):
    cfg = AssayConfig(
        massive=MassiveConfig(api_key="x", s3_access_key_id="x", s3_secret_access_key="x"),
        data_dir=tmp_path, market="US",
    )
    price = pl.DataFrame(price_rows, schema=_PRICE_SCHEMA)
    for (y, m) in {(d.year, d.month) for d in price["date"].to_list()}:
        part = price.filter((pl.col("date").dt.year() == y) & (pl.col("date").dt.month() == m))
        p = price_partition_path(tmp_path, "US", y, m)
        p.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(p)
    epath = adj_events_path(tmp_path, "US")
    epath.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(event_rows, schema=_EVENT_SCHEMA).write_parquet(epath)
    return DataStore(cfg)


def _price_row(d, sym, close, as_of=None):
    return {
        "date": d, "symbol": sym, "open": close, "high": close, "low": close,
        "close": close, "volume": 1.0e6, "transactions": 100,
        "as_of_date": as_of or d, "source_id": "t",
    }


def _event_row(sym, ex, as_of, ratio=1.0, cash=0.0):
    et = "DIVIDEND" if cash else "SPLIT"
    return {
        "symbol": sym, "ex_date": ex, "as_of_date": as_of, "event_type": et,
        "split_ratio": ratio, "dividend_cash": cash, "provider_adj_factor": 1.0,
        "event_id": f"{sym}-{ex}", "source": "t",
    }


def test_datastore_split_after_window_end_does_not_leak(tmp_path):
    # a split whose EX-DATE is after the query window must not adjust in-window bars,
    # even though it is "known" (as_of in the past). Guards against look-ahead.
    days = [dt.date(2024, 1, d) for d in range(2, 27)]
    rows = [_price_row(d, "AAA", 100.0) for d in days]
    events = [_event_row("AAA", dt.date(2024, 2, 15), dt.date(2024, 1, 1), ratio=2.0)]
    store = _build_store(tmp_path, rows, events)
    close = store.get_panel(
        fields=["close"], symbols=["AAA"],
        start_date="2024-01-02", end_date="2024-01-26",
        as_of_date="2024-07-01", adj="split",
    ).sort("date")["close"].to_numpy()
    np.testing.assert_allclose(close, 100.0, rtol=1e-5)  # NOT 50 — future split excluded


def test_datastore_total_return_dividend_path(tmp_path):
    # close drops 100 -> 99 purely from a $1 dividend on 2024-01-10
    days = [dt.date(2024, 1, d) for d in range(2, 27)]
    rows = [_price_row(d, "AAA", 100.0 if d < dt.date(2024, 1, 10) else 99.0) for d in days]
    events = [_event_row("AAA", dt.date(2024, 1, 10), dt.date(2024, 1, 10), cash=1.0)]
    store = _build_store(tmp_path, rows, events)

    def fetch(adj):
        return store.get_panel(
            fields=["close"], symbols=["AAA"],
            start_date="2024-01-02", end_date="2024-01-26",
            as_of_date="2024-01-31", adj=adj,
        ).sort("date")["close"].to_numpy()

    total = fetch("total")
    assert max_log_jump(total) < 1e-6                 # dividend removed -> flat 99
    np.testing.assert_allclose(total, 99.0, rtol=1e-5)
    assert max_log_jump(fetch("split")) > 0.009       # split mode keeps the dividend drop


def test_forward_adjust_handles_multiple_symbols_independently():
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(20)]
    rows = []
    for i, d in enumerate(days):
        rows.append({"date": d, "symbol": "AAA", "close": 200.0 if i < 10 else 100.0, "volume": 1000.0})
        rows.append({"date": d, "symbol": "BBB", "close": 50.0, "volume": 1000.0})  # no events
    prices = _prices(rows)
    events = _events([{"symbol": "AAA", "ex_date": days[10], "split_ratio": 2.0, "dividend_cash": 0.0}])
    out = forward_adjust(prices, events, mode="split")
    aaa = out.filter(pl.col("symbol") == "AAA").sort("date")["close"].to_numpy()
    bbb = out.filter(pl.col("symbol") == "BBB").sort("date")["close"].to_numpy()
    np.testing.assert_allclose(aaa, 100.0, rtol=1e-9)   # AAA split removed -> flat 100
    np.testing.assert_allclose(bbb, 50.0, rtol=1e-9)    # BBB untouched by AAA's split


def test_forward_adjust_invalid_mode_raises():
    with pytest.raises(ValueError, match="adjustment mode"):
        forward_adjust(_prices([{"date": DAYS[0], "symbol": "A", "close": 1.0, "volume": 1.0}]),
                       _events([]), mode="bogus")


def test_get_panel_requires_as_of_and_known_field(tmp_path):
    days = [dt.date(2024, 1, d) for d in range(2, 10)]
    store = _build_store(tmp_path, [_price_row(d, "AAA", 100.0) for d in days], [])
    with pytest.raises(ValueError, match="as_of_date is required"):
        store.get_panel(fields=["close"], symbols=["AAA"],
                        start_date="2024-01-02", end_date="2024-01-09", as_of_date=None)
    with pytest.raises(ValueError, match="unsupported field"):
        store.get_panel(fields=["vwap"], symbols=["AAA"],
                        start_date="2024-01-02", end_date="2024-01-09", as_of_date="2024-01-09")
