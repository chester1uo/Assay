"""A-share price-limit wiring in the portfolio backtester (multi-market support).

Offline only: ``FactorEngine.from_store`` is monkeypatched to wrap a synthetic
panel, and a fake store supplies ``get_trade_status`` (the real DataStore path is
covered by the data-layer tests). Verifies that

* :meth:`PortfolioBacktester._cn_limit_matrices` rebases the *raw* 涨停价/跌停价
  into the panel's adjusted basis (multiplying by ``adj_close / raw_close``), and
* a name locked at its ceiling on the execution bar has its *buy* blocked, so the
  report's A-share metrics record the limit hit.

Run with ``PYTHONPATH=src python -m pytest tests/portfolio/test_cn_limits.py -q``.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine
from assay.portfolio import PortfolioBacktestConfig, PortfolioBacktester


def _a_cfg(**kw) -> PortfolioBacktestConfig:
    base = dict(
        period_start="2021-01-01",
        period_end="2021-12-31",
        universe="CSI300",
        rebalance_type="daily",
        weight_method="equal",
        min_stock_count=5,
        save_trade_log=True,
    )
    base.update(kw)
    return PortfolioBacktestConfig.preset("A", **base)


class _FakeStore:
    def __init__(self, ts: pl.DataFrame) -> None:
        self._ts = ts

    def get_trade_status(self, symbols, start, end, as_of):  # mirrors DataStore
        return self._ts


def test_cn_limit_matrices_rebases_raw_bands_to_panel_basis():
    """Raw limit bands are scaled by f = adj_close/raw_close (here f == 2)."""
    dates = [dt.date(2021, 1, 1), dt.date(2021, 1, 2)]
    symbols = ["S0", "S1"]
    close_adj = np.array([[10.0, 20.0], [11.0, 22.0]])  # adjusted panel close
    ts = pl.DataFrame(
        {
            "date": [dates[0], dates[0], dates[1], dates[1]],
            "symbol": ["S0", "S1", "S0", "S1"],
            "up_limit": [5.5, 11.0, 6.05, 12.1],  # raw ceiling
            "down_limit": [4.5, 9.0, 4.95, 9.9],  # raw floor
            "close": [5.0, 10.0, 5.5, 11.0],  # raw close == half of adj -> f = 2
        }
    )
    bt = PortfolioBacktester(store=_FakeStore(ts))
    up, dn = bt._cn_limit_matrices(_a_cfg(), close_adj, dates, symbols, "2021-12-31")
    assert np.allclose(up, np.array([[11.0, 22.0], [12.1, 24.2]]))  # raw_up * 2
    assert np.allclose(dn, np.array([[9.0, 18.0], [9.9, 19.8]]))  # raw_down * 2


def test_cn_limit_matrices_aligns_numpy_datetime64_dates():
    """Engine dates are numpy ``datetime64``; trade_status dates are python
    ``date``. The grid must still align (regression: a type mismatch silently
    populated zero cells, so limits never fired)."""
    dates = [np.datetime64("2021-01-01"), np.datetime64("2021-01-02")]
    symbols = ["S0"]
    close_adj = np.array([[10.0], [10.0]])
    ts = pl.DataFrame(
        {
            "date": [dt.date(2021, 1, 1), dt.date(2021, 1, 2)],
            "symbol": ["S0", "S0"],
            "up_limit": [11.0, 11.0],
            "down_limit": [9.0, 9.0],
            "close": [10.0, 10.0],  # f == 1
        }
    )
    bt = PortfolioBacktester(store=_FakeStore(ts))
    up, dn = bt._cn_limit_matrices(_a_cfg(), close_adj, dates, symbols, "2021-12-31")
    assert np.isfinite(up).all() and np.allclose(up, 11.0)
    assert np.isfinite(dn).all() and np.allclose(dn, 9.0)


def test_cn_limit_matrices_noop_for_us_or_no_store():
    bt = PortfolioBacktester(store=None)
    cfg_a = _a_cfg()
    out = bt._cn_limit_matrices(cfg_a, np.ones((2, 2)), [dt.date(2021, 1, 1), dt.date(2021, 1, 2)], ["S0", "S1"], "2021-12-31")
    assert out == (None, None)  # market A but no store
    # US market with a store still no-ops (limits are A-share only)
    ts = pl.DataFrame({"date": [], "symbol": [], "up_limit": [], "down_limit": [], "close": []})
    bt_us = PortfolioBacktester(store=_FakeStore(ts))
    cfg_us = PortfolioBacktestConfig(period_start="2021-01-01", period_end="2021-12-31", market="US", universe="NASDAQ100")
    assert bt_us._cn_limit_matrices(cfg_us, np.ones((2, 2)), [dt.date(2021, 1, 1)], ["S0"], "2021-12-31") == (None, None)


def _ohlc_panel(dates, symbols, close, open_):
    n = len(symbols)
    rows = {
        "date": np.repeat(np.array(dates), n),
        "symbol": symbols * len(dates),
        "open": open_.reshape(-1),
        "high": (np.maximum(open_, close) + 0.5).reshape(-1),
        "low": (np.minimum(open_, close) - 0.5).reshape(-1),
        "close": close.reshape(-1),
        "volume": np.full(close.size, 1e6),
    }
    return pl.DataFrame(rows)


def test_a_share_long_only_guard():
    """market='A' refuses a short book (融券 restricted); US permits it."""
    with pytest.raises(ValueError, match="long-only"):
        PortfolioBacktestConfig.preset(
            "A", period_start="2021-01-01", period_end="2021-12-31", long_short=True
        )
    # US is unaffected — a long/short book is fine there.
    PortfolioBacktestConfig.preset(
        "US", period_start="2021-01-01", period_end="2021-12-31", long_short=True
    )


def test_adj_basis_derived_from_market():
    """A/HK backtests use total-return; US keeps split (tied to the hashed market)."""
    def mk(m):
        return PortfolioBacktestConfig.preset(m, period_start="2021-01-01", period_end="2021-12-31")

    assert PortfolioBacktester._adj(mk("A")) == "total"
    assert PortfolioBacktester._adj(mk("HK")) == "total"
    assert PortfolioBacktester._adj(mk("US")) == "split"


def test_load_groups_aligns_and_defaults_unknown():
    """Industry labels align to the symbol axis; missing symbols -> 'UNKNOWN'."""
    class _GStore:
        def get_groups(self, symbols, as_of):
            return {"S0": "银行", "S2": "白酒"}

    g = PortfolioBacktester(store=_GStore())._load_groups(["S0", "S1", "S2"], "2021-12-31")
    assert list(g) == ["银行", "UNKNOWN", "白酒"]
    # no store / no getter -> None (US / offline runs stay un-neutralised)
    assert PortfolioBacktester(store=None)._load_groups(["S0"], "2021-12-31") is None


def test_cn_backtest_blocks_buy_at_limit_up(monkeypatch):
    """A name locked limit-up on the execution bar cannot be bought (limit hit)."""
    T, N = 6, 6
    dates = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(T)]
    symbols = [f"S{j}" for j in range(N)]
    rng = np.random.default_rng(0)
    close = 100.0 + rng.normal(0, 1, (T, N)).cumsum(axis=0)
    open_ = close.copy()  # exec at next_open == close-ish; deterministic bands below
    panel = _ohlc_panel(dates, symbols, close, open_)

    monkeypatch.setattr(
        FactorEngine, "from_store", staticmethod(lambda *a, **k: FactorEngine(panel))
    )

    # trade_status: raw close == panel close (f == 1). Ceiling = +inf everywhere
    # except S0 on the first execution bar (t=1), where ceiling == that open so a
    # buy is blocked. Floor = -inf (never binds).
    big = 1e9
    recs = []
    for ti, d in enumerate(dates):
        for j, s in enumerate(symbols):
            up = open_[ti, j] if (ti == 1 and j == 0) else big
            recs.append({"date": d, "symbol": s, "up_limit": float(up),
                         "down_limit": -big, "close": float(close[ti, j])})
    ts = pl.DataFrame(recs)

    bt = PortfolioBacktester(store=_FakeStore(ts))
    report = bt.run("close", _a_cfg())

    assert report.a_share_metrics is not None
    assert report.a_share_metrics["n_limit_hits"] >= 1
    # the block is recorded against S0 as a limit_up reason
    blocked = [t for t in report.trade_log if t.blocked_reason == "limit_up"]
    assert any(t.symbol == "S0" for t in blocked)
