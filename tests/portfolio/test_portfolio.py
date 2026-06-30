"""End-to-end tests for the portfolio backtest module (design-doc Phase 5).

Offline only — no network, no DataStore. Synthetic ``(T, N)`` panels are built
with numpy, and :meth:`FactorEngine.from_store` is monkeypatched (as in the
service self-check) so the assembled :class:`PortfolioBacktester` never touches
data or credentials. ``conftest`` auto-tags everything in this folder with the
``portfolio`` marker.

Coverage mirrors the task contract — config, report, signal processing, weight
construction, constraints, rebalance scheduling, costs/execution (A-share rules),
accounting/metrics, and the assembled backtester integration.

Run with::

    PYTHONPATH=src python -m pytest tests/portfolio -q
"""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine
from assay.portfolio import (
    PortfolioBacktestConfig,
    PortfolioBacktester,
    PortfolioReport,
)
from assay.portfolio import constraints as C
from assay.portfolio import metrics as M
from assay.portfolio import signal as S
from assay.portfolio import weights as W
from assay.portfolio.accounting import PortfolioAccountant
from assay.portfolio.costs import TransactionCostModel
from assay.portfolio.execution import ExecutionSimulator
from assay.portfolio.report import PortfolioLineage, Trade


# ===========================================================================
# shared helpers / fixtures
# ===========================================================================
def _cfg(**kw) -> PortfolioBacktestConfig:
    base = dict(period_start="2021-01-01", period_end="2021-12-31")
    base.update(kw)
    return PortfolioBacktestConfig(**base)


def make_ohlcv_panel(t: int = 80, n: int = 10, *, seed: int = 1) -> pl.DataFrame:
    """Long (date, symbol, O/H/L/C, volume) panel from a random-walk close (T, N)."""
    rng = np.random.default_rng(seed)
    close = 100.0 + rng.normal(0.0, 1.0, (t, n)).cumsum(axis=0)
    open_ = close + rng.normal(0.0, 0.2, (t, n))
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.1, (t, n)))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.1, (t, n)))
    volume = 1e6 + np.abs(rng.normal(0.0, 1.0, (t, n))) * 1e4
    dates = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(t)]
    syms = [f"S{j}" for j in range(n)]
    return pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), n),
            "symbol": syms * t,
            "open": open_.reshape(-1),
            "high": high.reshape(-1),
            "low": low.reshape(-1),
            "close": close.reshape(-1),
            "volume": volume.reshape(-1),
        }
    )


@pytest.fixture
def patched_engine(monkeypatch):
    """Patch ``FactorEngine.from_store`` to build an engine from a synthetic panel.

    Returns a setter ``use(panel)`` so a test can install its own panel; defaults
    to a 80x10 OHLCV panel. Mirrors the backtester's call signature
    ``from_store(store, universe, (start, end), as_of=..., adj=...)``.
    """
    holder = {"panel": make_ohlcv_panel()}

    def fake_from_store(store, universe, period, as_of=None, adj="split", **kw):
        return FactorEngine(holder["panel"])

    monkeypatch.setattr(FactorEngine, "from_store", staticmethod(fake_from_store))

    def use(panel: pl.DataFrame) -> None:
        holder["panel"] = panel

    return use


# ===========================================================================
# 1. config  (design-doc section 2)
# ===========================================================================
def test_config_defaults_match_spec():
    c = _cfg()
    # 2.1/2.3/2.4/2.5/2.7 spec defaults (grounded: market/universe = US/NASDAQ100)
    assert c.market == "US" and c.universe == "NASDAQ100"
    assert c.rebalance_type == "monthly" and c.rebalance_day == "last"
    assert c.execution_offset_days == 1
    assert c.weight_method == "signal_prop" and c.signal_transform == "rank"
    assert c.gross_exposure == 1.0 and c.net_exposure == 1.0
    assert c.max_single_weight == 0.05 and c.min_stock_count == 10
    assert c.cov_method == "ledoit_wolf" and c.cov_window == 252
    assert c.execution_price == "next_open" and c.slippage_model == "sqrt"


def test_config_post_init_rejects_out_of_range():
    # max_single_weight valid range is [0.01, 0.30]; 0.99 must raise
    with pytest.raises(ValueError):
        _cfg(max_single_weight=0.99)
    # period ordering and required fields
    with pytest.raises(ValueError):
        PortfolioBacktestConfig(period_start="", period_end="")
    with pytest.raises(ValueError):
        _cfg(period_start="2021-12-31", period_end="2021-01-01")
    # enum validation
    with pytest.raises(ValueError):
        _cfg(weight_method="nope")
    # execution_offset_days must be >= 1 (0 is look-ahead bias)
    with pytest.raises(ValueError):
        _cfg(execution_offset_days=0)


def test_config_preset_A_sets_a_share_rules():
    a = PortfolioBacktestConfig.preset("A", period_start="2021-01-01", period_end="2021-12-31")
    assert a.market == "A"
    assert a.t_plus_1 is True
    assert a.price_limit_pct == 0.10
    assert a.enforce_limit_price is True
    assert a.stamp_duty_rate == 0.0005  # sell-side stamp duty (印花税, 0.05% since 2023-08)
    # US preset is the inverse: no T+1, no limit, no stamp duty
    us = PortfolioBacktestConfig.preset("US", period_start="2021-01-01", period_end="2021-12-31")
    assert us.t_plus_1 is False and us.price_limit_pct is None and us.stamp_duty_rate == 0.0


def test_config_to_from_dict_round_trip_and_hash_stable():
    c = _cfg(weight_method="mv", long_short=True, net_exposure=0.0, max_single_weight=0.10)
    d = c.to_dict()
    # JSON-serialisable
    json.dumps(d)
    rt = PortfolioBacktestConfig.from_dict(d)
    assert rt.to_dict() == d
    assert rt.config_hash() == c.config_hash()
    # config_hash is a stable 12-hex digest, identical across equal configs
    c2 = _cfg(weight_method="mv", long_short=True, net_exposure=0.0, max_single_weight=0.10)
    assert c.config_hash() == c2.config_hash()
    assert len(c.config_hash()) == 12
    # a differing field changes the hash
    assert c.config_hash() != _cfg(max_single_weight=0.11).config_hash()


def test_config_from_dict_ignores_unknown_keys():
    d = _cfg().to_dict()
    d["a_totally_unknown_key"] = 123
    rt = PortfolioBacktestConfig.from_dict(d)  # must not raise
    assert rt.period_start == "2021-01-01"


# ===========================================================================
# 2. report  (design-doc section 5)
# ===========================================================================
def _sample_report() -> PortfolioReport:
    return PortfolioReport(
        run_id="abc123def456",
        factor_id="deadbeef00000000",
        config=_cfg().to_dict(),
        period_start="2021-01-04",
        period_end="2021-03-31",
        n_trading_days=60,
        n_rebalances=3,
        total_return=0.12,
        annual_return=0.50,
        gross_return=0.14,
        excess_return=0.04,
        sharpe=1.8,
        sortino=float("nan"),       # NaN must serialise to None
        calmar=float("inf"),        # inf must serialise to None
        max_drawdown=0.08,
        max_drawdown_start="2021-02-01",
        max_drawdown_end="2021-02-15",
        drawdown_recovery_days=5,
        nav_series=[1.0, 1.05, float("nan"), 1.12],
        nav_dates=["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"],
        monthly_returns={"2021-01": 0.05, "2021-02": float("nan")},
        trade_log=[Trade("2021-01-04", "S0", "buy", 0.1, 0.1, 100.0, 0.1, 0.0003, None)],
        lineage=PortfolioLineage(data_snapshot="snap1", eval_timestamp="2021-04-01T00:00:00", adj_version="split"),
    )


def test_report_to_dict_json_serialisable_and_nan_to_none():
    rep = _sample_report()
    d = rep.to_dict()
    s = json.dumps(d)  # must not raise — fully JSON-safe
    assert isinstance(s, str)
    # NaN / inf scalar metrics map to None
    assert d["sortino"] is None
    assert d["calmar"] is None
    # NaN inside list / dict containers also maps to None
    assert d["nav_series"][2] is None
    assert d["monthly_returns"]["2021-02"] is None
    # finite values pass through unchanged
    assert d["sharpe"] == 1.8
    assert d["trade_log"][0]["symbol"] == "S0"


def test_report_from_dict_round_trip():
    rep = _sample_report()
    d = rep.to_dict()
    rt = PortfolioReport.from_dict(d)
    assert rt.run_id == rep.run_id
    assert rt.factor_id == rep.factor_id
    assert rt.n_rebalances == rep.n_rebalances
    assert rt.total_return == pytest.approx(rep.total_return)
    assert rt.max_drawdown_start == "2021-02-01"
    assert rt.lineage.adj_version == "split"
    assert len(rt.trade_log) == 1 and rt.trade_log[0].symbol == "S0"
    # to_dict is idempotent across the round-trip
    assert rt.to_dict() == d


def test_report_run_id_stable_and_deterministic():
    h = _cfg().config_hash()
    a = PortfolioReport.compute_run_id("factorX", h)
    b = PortfolioReport.compute_run_id("factorX", h)
    assert a == b and len(a) == 12
    # different factor or config -> different id
    assert PortfolioReport.compute_run_id("factorY", h) != a


# ===========================================================================
# 3. signal  (design-doc section 1.1 SignalProcessor / 2.4)
# ===========================================================================
def test_process_signal_rank_normalised_and_nan_aware():
    f = np.array([[3.0, 1.0, 2.0, np.nan], [np.nan, np.nan, np.nan, np.nan]])
    sig = S.process_signal(f, _cfg(signal_transform="rank"))
    # finite entries land in [0, 1]; the missing symbol stays NaN (never poisons)
    row0 = sig[0]
    assert np.isnan(row0[3])
    finite = row0[np.isfinite(row0)]
    assert finite.min() >= 0.0 and finite.max() <= 1.0
    # rank order preserved: 1.0 -> 0.0, 3.0 -> 1.0, 2.0 -> 0.5
    assert row0[1] == pytest.approx(0.0)
    assert row0[0] == pytest.approx(1.0)
    assert row0[2] == pytest.approx(0.5)
    # an all-NaN cross-section yields all-NaN, not a crash
    assert np.isnan(sig[1]).all()


def test_winsorize_clips_extremes():
    x = np.array([[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 1000.0]])
    wz = S.winsorize(x, p=0.1)
    # the 1000 outlier is clipped down to within the [10%, 90%] band
    assert wz.max() < 1000.0
    # NaNs are preserved untouched
    xn = np.array([[1.0, np.nan, 3.0, 100.0]])
    assert np.isnan(S.winsorize(xn, p=0.25)[0, 1])


def test_neutralize_noop_when_groups_none_and_active_with_groups():
    x = np.array([[1.0, 2.0, 3.0, 4.0]])
    # No sector data (groups None) -> documented no-op (never fabricates labels)
    assert np.array_equal(S.neutralize(x, None), x)
    # With groups, each group's per-date mean is removed (within-group residual)
    groups = np.array([0, 0, 1, 1])
    out = S.neutralize(x, groups)
    # group 0 mean=1.5 -> [-0.5, 0.5]; group 1 mean=3.5 -> [-0.5, 0.5]
    assert out[0].tolist() == pytest.approx([-0.5, 0.5, -0.5, 0.5])


# ===========================================================================
# 4. weights  (design-doc section 2.4 / 3.3)
# ===========================================================================
def test_equal_and_signal_prop_scaling_long_only():
    sig = np.linspace(0.0, 1.0, 8)
    ew = W.equal_weight(sig, long_short=False, gross=1.0)
    assert ew.sum() == pytest.approx(1.0)
    assert np.allclose(ew, 1.0 / 8)  # equal-weight
    sp = W.signal_prop(sig, long_short=False, gross=1.0)
    assert sp.sum() == pytest.approx(1.0)
    assert (sp >= 0).all()
    # signal_prop tilts toward higher signal: top name outweighs the bottom
    assert sp[-1] > sp[0]


def test_quantile_long_short_opposite_sign_legs():
    sig = np.linspace(0.0, 1.0, 20)
    w = W.quantile_weight(sig, long_n=1, short_n=1, n_groups=5, long_short=True, gross=1.0, net=0.0)
    assert (w > 0).any() and (w < 0).any()        # both legs present
    assert w[-1] > 0 and w[0] < 0                 # top long, bottom short
    assert np.abs(w).sum() == pytest.approx(1.0)  # gross budget
    assert w.sum() == pytest.approx(0.0, abs=1e-9)  # dollar-neutral net


def test_mean_variance_feasible_no_cvxpy():
    rng = np.random.default_rng(0)
    n = 8
    rw = rng.normal(0.0, 0.01, (120, n))
    alpha = S.normalize(np.linspace(0, 1, n).reshape(1, -1), "rank").ravel()
    cfg = _cfg(weight_method="mv", long_short=False, max_single_weight=0.30, net_exposure=1.0, cov_window=120)
    w = W.mean_variance(alpha, rw, np.zeros(n), cfg)
    assert np.isfinite(w).all()
    assert w.sum() == pytest.approx(1.0, abs=1e-4)   # budget == net_exposure
    assert w.max() <= 0.30 + 1e-6 and w.min() >= -1e-9  # box respected, long-only


def test_risk_parity_positive_long_only():
    rng = np.random.default_rng(2)
    rw = rng.normal(0.0, 0.01, (150, 6))
    w = W.risk_parity(rw, gross=1.0)
    nz = w[w != 0.0]
    assert (nz > 0).all()                # long-only, strictly positive holdings
    assert w.sum() == pytest.approx(1.0)  # scaled to gross


def test_ledoit_wolf_cov_symmetric_and_psd():
    rng = np.random.default_rng(3)
    rw = rng.normal(0.0, 0.01, (120, 8))
    cov = W.ledoit_wolf_cov(rw)
    assert cov.shape == (8, 8)
    assert np.allclose(cov, cov.T)                  # symmetric
    eig = np.linalg.eigvalsh(cov)
    assert eig.min() > -1e-12                        # PSD (shrinkage lifts the spectrum)
    # dead (all-NaN) column is dropped: marked NaN in the embedded matrix
    rw2 = rw.copy()
    rw2[:, 0] = np.nan
    cov2 = W.ledoit_wolf_cov(rw2)
    assert np.isnan(cov2[0, 0])
    live = cov2[1:, 1:]
    assert np.isfinite(live).all() and np.allclose(live, live.T)


# ===========================================================================
# 5. constraints  (design-doc section 2.5)
# ===========================================================================
def test_position_cap_respected():
    w = np.array([0.5, 0.3, 0.2, 0.0])
    out = C.apply_position_limits(w, max_single=0.25, min_single=0.0, long_short=False)
    assert out.max() <= 0.25 + 1e-12
    assert out.tolist() == pytest.approx([0.25, 0.25, 0.2, 0.0])


def test_turnover_cap_limits_one_way_turnover():
    cur = np.array([0.25, 0.25, 0.25, 0.25])
    tgt = np.array([0.50, 0.50, 0.00, 0.00])  # one-way turnover 0.5 unconstrained
    out = C.cap_turnover(tgt, cur, max_turnover=0.10)
    one_way = 0.5 * np.abs(out - cur).sum()
    assert one_way <= 0.10 + 1e-9
    # a non-binding cap (>= 1.0) is a no-op
    assert np.array_equal(C.cap_turnover(tgt, cur, 1.0), tgt)


def test_count_cap_keeps_at_most_max_names():
    w = np.array([0.10, 0.20, 0.30, 0.40, 0.05])
    out = C.cap_stock_count(w, min_count=0, max_count=2)
    assert int((out != 0).sum()) == 2
    # keeps the two largest by magnitude (0.40, 0.30)
    assert out[3] == 0.40 and out[2] == 0.30
    # None / <=0 max_count is a no-op
    assert np.array_equal(C.cap_stock_count(w, max_count=None), w)


def test_exposure_scaling_hits_gross_and_net():
    # long-only: sum hits gross
    w = np.array([0.3, 0.2, 0.1, 0.0])
    lo = C.scale_exposure(w, gross=1.0, long_short=False)
    assert lo.sum() == pytest.approx(1.0)
    # long/short: gross == sum|w|, net == sum w hit independently
    ws = np.array([0.3, 0.2, -0.1, -0.4])
    ls = C.scale_exposure(ws, gross=2.0, net=0.0, long_short=True)
    assert np.abs(ls).sum() == pytest.approx(2.0)
    assert ls.sum() == pytest.approx(0.0, abs=1e-9)


def test_apply_constraints_jointly_feasible():
    rng = np.random.default_rng(4)
    raw = np.abs(rng.normal(0, 1, 30))  # 30 long-only candidates
    cfg = _cfg(max_single_weight=0.10, min_stock_count=10, gross_exposure=1.0)
    out = C.apply_constraints(raw, np.zeros(30), cfg)
    assert out.max() <= 0.10 + 1e-9            # per-stock cap
    assert out.sum() == pytest.approx(1.0, abs=1e-6)  # gross preserved after capping
    # all-zero target -> zero vector of the right length (feasible by construction)
    z = C.apply_constraints(np.zeros(5), np.zeros(5), cfg)
    assert z.shape == (5,) and not np.any(z)


# ===========================================================================
# 6. rebalance  (design-doc section 3)
# ===========================================================================
def _business_days(n: int, start=dt.date(2021, 1, 4)) -> list[dt.date]:
    out: list[dt.date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = d + dt.timedelta(days=1)
    return out


def test_monthly_rebalance_about_one_per_month():
    from assay.portfolio import rebalance as rb

    dates = _business_days(70)  # ~3.5 calendar months
    idx = rb.calendar_dates(dates, "monthly", "last")
    months = {(dates[i].year, dates[i].month) for i in range(len(dates))}
    assert len(idx) == len(months)  # exactly one rebalance per calendar month


def test_min_rebalance_interval_respected():
    from assay.portfolio import rebalance as rb

    dates = _business_days(60)
    cfg = _cfg(rebalance_type="daily", min_rebalance_interval=10)
    idx = rb.rebalance_dates(np.zeros((60, 5)), dates, cfg)
    assert all(b - a >= 10 for a, b in zip(idx, idx[1:]))


def test_signal_autocorr_triggers_on_shifting_factor():
    from assay.portfolio import rebalance as rb

    rng = np.random.default_rng(0)
    T, N = 60, 30
    base = rng.normal(0, 1, N)
    factor = np.empty((T, N))
    for t in range(T):
        # stable ranking in the first half, churning (reshuffled) in the second
        factor[t] = base + rng.normal(0, 0.01, N) if t < 30 else rng.normal(0, 1, N)
    cfg = _cfg(rebalance_type="signal", signal_autocorr_floor=0.7, min_rebalance_interval=5)
    idx = rb.signal_autocorr_dates(factor, _business_days(T), cfg)
    assert idx                       # the churn triggers rebalances
    assert all(i >= 30 for i in idx)  # only in the unstable second half
    assert all(b - a >= 5 for a, b in zip(idx, idx[1:]))  # interval honoured


# ===========================================================================
# 7. costs / execution  (design-doc section 2.6.1 / 2.6.2 / 2.6.3 / 2.7)
# ===========================================================================
def test_a_share_sell_cost_exceeds_buy():
    cfg = PortfolioBacktestConfig.preset("A", period_start="2021-01-01", period_end="2021-12-31")
    cm = TransactionCostModel(cfg)
    buy = cm.trade_cost("buy", 0.02)
    sell = cm.trade_cost("sell", 0.02)
    # sell carries the stamp duty (印花税) the buy does not -> strictly costlier
    assert sell > buy
    assert sell - buy == pytest.approx(cfg.stamp_duty_rate)


def test_price_limit_blocks_buy_at_limit_up():
    cfg = PortfolioBacktestConfig.preset("A", period_start="2021-01-01", period_end="2021-12-31")
    sim = ExecutionSimulator(cfg)
    prev = np.array([100.0, 100.0, 100.0])
    price = np.array([110.0, 100.0, 100.0])  # name 0 at limit-up (+10%)
    res = sim.execute(
        np.array([0.3, 0.3, 0.3]),
        np.zeros(3),
        exec_price=price,
        prev_close=prev,
        acquired_step={},
        step=0,
        symbols=["A", "B", "C"],
        date="d",
    )
    blocked = {t.symbol: t.blocked_reason for t in res.trades if t.blocked_reason}
    assert blocked.get("A") == "limit_up"
    assert res.diag["limit_hit_count"] == 1.0
    assert res.executed_weights[0] == 0.0  # the blocked buy did not fill


def test_adv_cap_defers_excess():
    cfg = PortfolioBacktestConfig.preset(
        "A", period_start="2021-01-01", period_end="2021-12-31",
        max_adv_fraction=0.10, partial_fill_handling="defer",
    )
    sim = ExecutionSimulator(cfg)
    adv = np.array([0.50, 0.01, 0.01])  # name 0's participation is 5x the cap
    res = sim.execute(
        np.array([0.30, 0.30, 0.30]),
        np.zeros(3),
        exec_price=np.array([100.0, 100.0, 100.0]),
        adv_fraction=adv,
        acquired_step={},
        step=0,
        symbols=["A", "B", "C"],
        date="d",
    )
    # name 0 fills only max_adv/adv = 0.10/0.50 = 20% of the 0.30 target -> 0.06
    assert res.executed_weights[0] == pytest.approx(0.06, abs=1e-9)
    assert res.residual_targets[0] == pytest.approx(0.30)  # the rest is deferred


def test_t_plus_1_defers_same_name_sell_when_market_A():
    cfg = PortfolioBacktestConfig.preset("A", period_start="2021-01-01", period_end="2021-12-31")
    sim = ExecutionSimulator(cfg)
    acq: dict[int, int] = {}
    px = np.array([100.0, 100.0, 100.0])
    # step 0: buy name 0
    r0 = sim.execute(
        np.array([0.30, 0.0, 0.0]), np.zeros(3),
        exec_price=px, acquired_step=acq, step=0, symbols=["A", "B", "C"], date="d0",
    )
    assert r0.executed_weights[0] == pytest.approx(0.30)
    # same step: try to sell name 0 -> T+1 forbids selling a same-step acquisition
    r1 = sim.execute(
        np.zeros(3), r0.executed_weights,
        exec_price=px, acquired_step=acq, step=0, symbols=["A", "B", "C"], date="d0",
    )
    assert r1.diag["forced_hold_count"] == 1.0
    assert {t.symbol: t.blocked_reason for t in r1.trades if t.blocked_reason}.get("A") == "t_plus_1"
    assert r1.executed_weights[0] == pytest.approx(0.30)  # forced to hold


def test_t_plus_1_off_for_us_market():
    cfg = PortfolioBacktestConfig.preset("US", period_start="2021-01-01", period_end="2021-12-31")
    sim = ExecutionSimulator(cfg)
    acq: dict[int, int] = {}
    px = np.array([100.0, 100.0])
    r0 = sim.execute(np.array([0.3, 0.0]), np.zeros(2), exec_price=px, acquired_step=acq, step=0)
    # US has no T+1: a same-step sell is allowed to fill (no forced hold)
    r1 = sim.execute(np.zeros(2), r0.executed_weights, exec_price=px, acquired_step=acq, step=0)
    assert r1.diag["forced_hold_count"] == 0.0
    assert r1.executed_weights[0] == pytest.approx(0.0)


# ===========================================================================
# 8. accounting / metrics  (design-doc section 4)
# ===========================================================================
def test_constant_positive_return_nav_rises_sharpe_positive_no_drawdown():
    cfg = _cfg(market="US")
    acct = PortfolioAccountant(cfg)
    T, N = 60, 4
    # near-constant *positive* daily return: NAV monotone up, tiny non-zero vol
    rng = np.random.default_rng(7)
    rets = 0.0010 + np.abs(rng.normal(0.0, 0.00005, (T, N)))  # strictly positive
    schedule = {0: np.full(N, 0.25)}
    res = acct.run(rets, schedule, exec_prices=np.ones((T, N)))
    nav = res.nav_series
    assert nav[-1] > nav[0]                       # NAV rises
    daily = M.returns_from_nav(nav)
    assert M.sharpe(daily) > 0                     # positive drift, non-zero vol
    mdd, *_ = M.max_drawdown(nav)
    assert np.isnan(mdd) or mdd < 1e-9             # never falls below a prior peak


def test_cost_drag_non_negative_gross_minus_net():
    cfg = PortfolioBacktestConfig.preset(
        "A", period_start="2021-01-01", period_end="2021-12-31", rebalance_type="weekly"
    )
    acct = PortfolioAccountant(cfg)
    T, N = 40, 5
    rng = np.random.default_rng(8)
    rets = rng.normal(0.0005, 0.01, (T, N))
    # rebalance on several dates so costs actually accrue
    target = np.full(N, 1.0 / N)
    schedule = {t: target for t in (0, 10, 20, 30)}
    res = acct.run(rets, schedule, exec_prices=np.ones((T, N)))
    drag = PortfolioAccountant.cost_drag(res.nav_series, res.gross_nav)
    assert np.isfinite(drag)
    assert drag >= -1e-12  # net <= gross; costs only ever drag NAV down


def test_monthly_returns_keys_are_yyyy_mm():
    rng = np.random.default_rng(9)
    r = rng.normal(0.0005, 0.005, 74)
    nav = np.concatenate([[1.0], np.cumprod(1.0 + r)])
    dates = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(nav.size)]
    mr = M.monthly_returns(nav, dates)
    assert mr  # non-empty
    for k, v in mr.items():
        assert len(k) == 7 and k[4] == "-"        # 'YYYY-MM'
        int(k[:4]) and int(k[5:])                  # parseable
        assert v is None or np.isfinite(v)
    assert set(mr) <= {"2021-01", "2021-02", "2021-03"}


# ===========================================================================
# 9. integration  (PortfolioBacktester.run, design-doc section 1.1)
# ===========================================================================
def test_backtester_run_produces_well_formed_report(patched_engine):
    cfg = _cfg(
        period_start="2021-01-01", period_end="2021-03-31",
        market="US", rebalance_type="monthly", weight_method="signal_prop",
    )
    rep = PortfolioBacktester(store=None).run("close", cfg)
    # NAV series spans the panel; rebalances actually happened
    assert len(rep.nav_series) == len(rep.nav_dates) == 80
    assert rep.n_trading_days == 80
    assert rep.n_rebalances > 0
    # headline metrics are finite (not NaN/inf) on this well-behaved panel
    assert np.isfinite(rep.total_return)
    assert np.isfinite(rep.sharpe)
    assert np.isfinite(rep.max_drawdown) or rep.max_drawdown is None or np.isnan(rep.max_drawdown)
    # JSON round-trip via the report schema
    d = rep.to_dict()
    json.dumps(d)  # serialises cleanly
    rt = PortfolioReport.from_dict(d)
    assert rt.run_id == rep.run_id and rt.n_rebalances == rep.n_rebalances
    # identity is a stable function of factor + config
    rep2 = PortfolioBacktester(store=None).run("close", cfg)
    assert rep2.run_id == rep.run_id


def test_backtester_long_short_and_mv_paths(patched_engine):
    # exercise a long/short quantile book and the scipy-SLSQP mean-variance path
    for wm, ls, net in (("quintile", True, 0.0), ("mv", False, 1.0)):
        cfg = _cfg(
            period_start="2021-01-01", period_end="2021-03-31", market="US",
            weight_method=wm, long_short=ls, net_exposure=net, max_single_weight=0.30,
            cov_window=60,
        )
        rep = PortfolioBacktester(store=None).run("ts_mean(close, 5)", cfg)
        assert rep.n_rebalances > 0
        assert len(rep.nav_series) == 80
        assert np.isfinite(rep.total_return)


def test_backtester_all_nan_factor_yields_graceful_empty_report(patched_engine):
    # a window longer than the panel produces an all-NaN factor -> no eligible names
    cfg = _cfg(period_start="2021-01-01", period_end="2021-03-31", market="US")
    rep = PortfolioBacktester(store=None).run("ts_mean(close, 500)", cfg)
    # well-formed but empty: no NAV, no rebalances, a diagnostic note, no raise
    assert rep.n_rebalances == 0
    assert rep.nav_series == []
    assert rep.attribution is not None and "note" in rep.attribution
    assert np.isnan(rep.total_return)
    json.dumps(rep.to_dict())  # the empty report still serialises


def test_backtester_a_share_metrics_present_only_when_market_A(patched_engine):
    us = PortfolioBacktester(store=None).run(
        "close", _cfg(period_start="2021-01-01", period_end="2021-03-31", market="US")
    )
    assert us.a_share_metrics is None
    a = PortfolioBacktester(store=None).run(
        "close",
        PortfolioBacktestConfig.preset(
            "A", period_start="2021-01-01", period_end="2021-03-31", universe="NASDAQ100",
        ),
    )
    assert a.a_share_metrics is not None
    # data-dependent A-share metrics are reported None, never fabricated
    assert a.a_share_metrics["suspension_impact"] is None
    assert a.a_share_metrics["northbound_flow_corr"] is None
    json.dumps(a.to_dict())


# ===========================================================================
# 10. regression — rebalance/turnover faithfulness against the live drifted book
# ===========================================================================
def _flat_panel_constant_factor(t: int = 60, n: int = 12) -> pl.DataFrame:
    """Flat market (no drift) + a per-name constant factor in the ``open`` column.

    ``close`` is identical every day (zero returns, so the book never drifts) and
    ``open`` encodes a stable cross-sectional rank (column index), so a factor of
    ``open`` has zero rank-shift and a target that never drifts from the book.
    """
    close = np.full((t, n), 100.0)
    open_ = (100.0 + np.arange(n)[None, :]).repeat(t, axis=0).astype(float)
    dates = [dt.date(2021, 1, 4) + dt.timedelta(days=i) for i in range(t)]
    syms = [f"S{j}" for j in range(n)]
    return pl.DataFrame({
        "date": np.repeat(np.array(dates), n), "symbol": syms * t,
        "open": open_.reshape(-1), "high": (open_ + 1).reshape(-1),
        "low": (open_ - 1).reshape(-1), "close": close.reshape(-1),
        "volume": np.full((t, n), 1e6).reshape(-1),
    })


def test_threshold_rebalance_respects_gate_on_stable_factor(patched_engine):
    # Regression: the threshold gate must compare the new *target weights* to the
    # live book — not the raw signal. On a stable factor + flat market the book
    # never drifts, so after the first establish the gate must hold (1 rebalance).
    patched_engine(_flat_panel_constant_factor())
    cfg = _cfg(
        period_start="2021-01-04", period_end="2021-12-31", market="US",
        rebalance_type="threshold", weight_method="signal_prop", long_short=False,
        threshold_rank_shift=2, threshold_weight_drift=0.05, signal_transform="rank",
        min_rebalance_interval=1, execution_offset_days=1, execution_price="next_close",
        max_single_weight=0.30, min_stock_count=5, commission_rate=0.0001,
        commission_min=0.0, transfer_fee_rate=0.00001, stamp_duty_rate=0.0,
        slippage_model="zero", benchmark="none",
    )
    rep = PortfolioBacktester(store=None).run("open", cfg)
    # Exactly one rebalance: establish once, then the gate holds every later day.
    assert rep.n_rebalances == 1


def test_turnover_cap_enforced_against_drifted_book(patched_engine, monkeypatch):
    # Regression: realised per-rebalance one-way turnover must respect
    # max_turnover_per_period measured against the DRIFTED book (apply_constraints
    # now diffs the target vs the live position, not a stale prior target).
    rng = np.random.default_rng(3)
    t, n = 120, 25
    rets = rng.normal(0.0, 0.03, (t, n))  # volatile -> large drift between rebalances
    close = 100.0 * np.cumprod(1 + rets, axis=0)
    dates = [dt.date(2021, 1, 4) + dt.timedelta(days=i) for i in range(t)]
    syms = [f"S{j}" for j in range(n)]
    panel = pl.DataFrame({
        "date": np.repeat(np.array(dates), n), "symbol": syms * t,
        "open": close.reshape(-1), "high": (close + 1).reshape(-1),
        "low": (close - 1).reshape(-1), "close": close.reshape(-1),
        "volume": np.full((t, n), 1e6).reshape(-1),
    })
    patched_engine(panel)
    cap = 0.10
    cfg = _cfg(
        period_start="2021-01-04", period_end="2021-12-31", market="US",
        rebalance_type="monthly", weight_method="signal_prop", long_short=False,
        signal_transform="rank", max_turnover_per_period=cap, execution_offset_days=1,
        execution_price="next_close", max_single_weight=0.10, min_stock_count=5,
        commission_rate=0.0001, commission_min=0.0, transfer_fee_rate=0.00001,
        stamp_duty_rate=0.0, slippage_model="zero", benchmark="none", save_trade_log=False,
    )
    captured = {}
    orig_run = PortfolioAccountant.run
    monkeypatch.setattr(
        PortfolioAccountant, "run",
        lambda self, *a, **k: captured.setdefault("res", orig_run(self, *a, **k)),
    )
    PortfolioBacktester(store=None).run("close", cfg)
    tpr = captured["res"].turnover_per_rebalance
    assert len(tpr) >= 2  # several monthly rebalances actually fired
    assert max(tpr) <= cap + 1e-9  # every rebalance respected the cap on the live book


def test_accountant_callable_schedule_uses_live_book_and_skips_on_none():
    # The accountant accepts a callable schedule entry, invokes it with the live
    # drifted book, and a None return means "no trade this date".
    cfg = _cfg(market="US", commission_rate=0.0001, commission_min=0.0,
               transfer_fee_rate=0.00001, stamp_duty_rate=0.0, slippage_model="zero")
    acct = PortfolioAccountant(cfg)
    T, N = 4, 2
    rets = np.zeros((T, N))
    rets[1] = [0.10, 0.0]  # asset0 +10% into day 1 so the day-2 book is drifted
    seen = {}

    def fn(cur_w):
        seen["cur_w"] = cur_w.copy()
        return None  # gate did not fire -> no trade

    # t=1 establishes 100% asset0 (array entry); t=2 is a callable that inspects the
    # drifted book and declines to trade.
    schedule = {1: np.array([1.0, 0.0]), 2: fn}
    res = acct.run(rets, schedule, exec_prices=np.ones((T, N)))
    # the callable saw the post-day-2 drifted book (still ~100% asset0, finite)
    assert "cur_w" in seen and np.isclose(seen["cur_w"].sum(), 1.0)
    # None return => only the t=1 rebalance is recorded
    assert res.rebalance_dates_idx == [1]
