"""Tests for portfolio performance metrics (design-doc section 4).

Offline, synthetic NAV/return arrays built with numpy. Run with::

    PYTHONPATH=src python -m pytest tests/portfolio/test_metrics.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from assay.portfolio import metrics as m
from assay.portfolio.config import PortfolioBacktestConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _nav_from_returns(r: np.ndarray, start: float = 1.0) -> np.ndarray:
    return np.concatenate([[start], start * np.cumprod(1.0 + np.asarray(r))])


def _trading_dates(n: int, start=dt.date(2020, 1, 1)) -> list[dt.date]:
    """n consecutive weekday-ish dates (calendar days fine for these unit tests)."""
    return [start + dt.timedelta(days=i) for i in range(n)]


# ---------------------------------------------------------------------------
# 4.1 return metrics
# ---------------------------------------------------------------------------
def test_returns_from_nav_basic_and_nan_aware():
    nav = np.array([1.0, 1.1, 1.21, 1.0])
    r = m.returns_from_nav(nav)
    assert np.allclose(r, [0.1, 0.1, 1.0 / 1.21 - 1.0])
    # NaN / non-positive prior NAV -> NaN, not a crash
    nav2 = np.array([1.0, np.nan, 2.0, 0.0, 1.0])
    r2 = m.returns_from_nav(nav2)
    assert np.isnan(r2[0]) and np.isnan(r2[1])  # neighbours of the NaN
    assert np.isnan(r2[3])  # prior NAV == 0


def test_total_and_annualized_return():
    # +0.05% daily for 252 steps -> 253-point NAV
    nav = _nav_from_returns(np.full(252, 0.0005))
    tot = m.total_return(nav)
    assert tot == pytest.approx(np.prod(1 + np.full(252, 0.0005)) - 1.0, rel=1e-9)
    ann = m.annualized_return(nav)
    # 252 compounding steps == exactly one year, so annual ~= total
    assert ann == pytest.approx(tot, rel=1e-6)


def test_annualized_return_scales_with_period():
    half = _nav_from_returns(np.full(126, 0.001))  # half a trading year
    ann = m.annualized_return(half)
    tot = m.total_return(half)
    # annual return should exceed the half-year total (compounded up)
    assert ann > tot > 0


def test_excess_return_vs_benchmark():
    port = _nav_from_returns(np.full(10, 0.01))
    bench = _nav_from_returns(np.full(10, 0.005))
    ex = m.excess_return(port, bench)
    assert ex == pytest.approx(m.total_return(port) - m.total_return(bench))
    assert np.isnan(m.excess_return(port, None))


def test_log_return_additive():
    nav = _nav_from_returns([0.1, -0.05, 0.2])
    expected = np.log(1.1) + np.log(0.95) + np.log(1.2)
    assert m.log_return(nav) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 4.2 risk-adjusted
# ---------------------------------------------------------------------------
def test_sharpe_finite_on_noisy_series_and_signs():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0005, 0.01, 252)
    s = m.sharpe(r, rf_annual=0.0)
    assert np.isfinite(s)
    # rf raises the bar -> lower Sharpe
    assert m.sharpe(r, rf_annual=0.05) < s


def test_sharpe_zero_variance_is_nan():
    # perfectly constant returns -> undefined Sharpe (zero vol), must be NaN
    assert np.isnan(m.sharpe(np.full(100, 0.001)))
    assert np.isnan(m.sharpe(np.array([0.01])))  # < 2 points


def test_sortino_only_penalises_downside():
    rng = np.random.default_rng(1)
    r = rng.normal(0.001, 0.01, 252)
    so = m.sortino(r, 0.0)
    sh = m.sharpe(r, 0.0)
    assert np.isfinite(so) and np.isfinite(sh)
    # positive-drift series: Sortino exceeds Sharpe (downside vol < total vol)
    assert so > sh


def test_max_drawdown_indices_and_recovery():
    nav = np.array([1.0, 1.2, 0.9, 1.0, 1.3])  # peak@1, trough@2, recover@4
    mdd, peak, trough, rec = m.max_drawdown(nav)
    assert mdd == pytest.approx(1 - 0.9 / 1.2)
    assert peak == 1 and trough == 2 and rec == 4


def test_max_drawdown_unrecovered_is_none():
    nav = np.array([1.0, 1.5, 1.2, 1.1, 1.0])  # never regains 1.5
    mdd, peak, trough, rec = m.max_drawdown(nav)
    assert mdd == pytest.approx(1 - 1.0 / 1.5)
    assert peak == 1 and trough == 4 and rec is None


def test_max_drawdown_monotone_up_is_nan():
    mdd, peak, trough, rec = m.max_drawdown(np.array([1.0, 1.1, 1.2, 1.3]))
    assert np.isnan(mdd) and peak == -1 and trough == -1 and rec is None


def test_max_drawdown_nan_aware():
    # the NaN in the middle must not poison the drawdown computation
    nav = np.array([1.0, 1.2, np.nan, 0.9, 1.0])
    mdd, peak, trough, rec = m.max_drawdown(nav)
    assert mdd == pytest.approx(1 - 0.9 / 1.2)
    assert trough == 3


def test_drawdown_duration():
    nav = np.array([1.0, 1.2, 0.9, 0.95, 1.0, 1.3])  # below peak for idx 2,3,4
    assert m.drawdown_duration(nav) == 3
    assert m.drawdown_duration(np.array([1.0, 1.1, 1.2])) == 0


def test_calmar():
    assert m.calmar(0.20, 0.10) == pytest.approx(2.0)
    assert np.isnan(m.calmar(0.20, 0.0))
    assert np.isnan(m.calmar(float("nan"), 0.1))


def test_beta_and_alpha():
    rng = np.random.default_rng(2)
    mkt = rng.normal(0.0, 0.01, 300)
    # port = 1.5*mkt + idiosyncratic noise + small alpha
    port = 1.5 * mkt + rng.normal(0.0002, 0.002, 300)
    b = m.beta(port, mkt)
    assert b == pytest.approx(1.5, abs=0.15)
    a = m.alpha_capm(port, mkt, rf_annual=0.0)
    assert np.isfinite(a) and a > 0  # positive built-in alpha, annualised
    assert np.isnan(m.beta(port, None))


def test_information_ratio_and_tracking_error():
    rng = np.random.default_rng(3)
    bench = rng.normal(0.0, 0.01, 300)
    port = bench + rng.normal(0.0005, 0.003, 300)  # consistent active return
    ir = m.information_ratio(port, bench)
    te = m.tracking_error(port, bench)
    assert np.isfinite(ir) and ir > 0
    assert np.isfinite(te) and te > 0
    assert np.isnan(m.information_ratio(port, None))
    assert np.isnan(m.tracking_error(port, None))


# ---------------------------------------------------------------------------
# 4.3 turnover and cost
# ---------------------------------------------------------------------------
def test_turnover_and_holding_period():
    at = m.annual_turnover(0.25, 12)  # 25% one-way, 12 rebalances/yr
    assert at == pytest.approx(3.0)
    # implied holding ~ 252 / 3 trading days
    assert m.avg_holding_days(at) == pytest.approx(252 / 3.0)
    assert np.isnan(m.avg_holding_days(0.0))


def test_cost_drag():
    assert m.cost_drag(0.20, 0.18) == pytest.approx(0.02)
    assert np.isnan(m.cost_drag(float("nan"), 0.1))


# ---------------------------------------------------------------------------
# monthly returns
# ---------------------------------------------------------------------------
def test_monthly_returns_keys_and_values():
    # 3 calendar months of daily data
    dates = []
    navs = [1.0]
    d = dt.date(2021, 1, 1)
    for _ in range(75):
        dates.append(d)
        d = d + dt.timedelta(days=1)
    # pad nav to match dates length
    rng = np.random.default_rng(4)
    navs = _nav_from_returns(rng.normal(0.0005, 0.005, len(dates) - 1))
    mr = m.monthly_returns(navs, dates)
    # spans Jan/Feb/Mar 2021
    assert set(mr) <= {"2021-01", "2021-02", "2021-03"}
    assert "2021-01" in mr
    for v in mr.values():
        assert v is None or np.isfinite(v)


def test_monthly_returns_handles_string_and_datetime64_dates():
    nav = _nav_from_returns([0.01, 0.02, -0.01])
    dates_str = ["2022-01-31", "2022-02-01", "2022-02-15", "2022-03-01"]
    mr = m.monthly_returns(nav, dates_str)
    assert "2022-02" in mr
    d64 = np.array(dates_str, dtype="datetime64[D]")
    mr2 = m.monthly_returns(nav, d64)
    assert mr.keys() == mr2.keys()


# ---------------------------------------------------------------------------
# headline reducer
# ---------------------------------------------------------------------------
def test_compute_metrics_full_dict():
    rng = np.random.default_rng(5)
    r = rng.normal(0.0006, 0.01, 252)
    nav = _nav_from_returns(r)
    gross = _nav_from_returns(r + 0.0001)  # gross slightly higher than net
    bench = _nav_from_returns(rng.normal(0.0003, 0.008, 252))
    dates = _trading_dates(nav.size)
    cfg = PortfolioBacktestConfig(
        period_start="2020-01-01", period_end="2020-12-31", risk_free_rate=0.015
    )
    out = m.compute_metrics(
        nav, dates, bench, cfg,
        gross_nav=gross, one_way_per_rebal=0.3, n_rebalances=12,
    )
    for key in (
        "total_return", "annual_return", "gross_return", "excess_return",
        "sharpe", "sortino", "calmar", "information_ratio", "max_drawdown",
        "beta", "alpha_capm", "tracking_error", "annual_turnover",
        "cost_drag", "avg_holding_days", "monthly_returns",
    ):
        assert key in out
    assert np.isfinite(out["sharpe"])
    assert out["cost_drag"] == pytest.approx(out["gross_return"] - out["total_return"])
    assert np.isfinite(out["annual_turnover"]) and out["annual_turnover"] > 0
    assert np.isfinite(out["avg_holding_days"])
    assert isinstance(out["monthly_returns"], dict) and out["monthly_returns"]


def test_compute_metrics_no_benchmark_no_turnover():
    nav = _nav_from_returns(np.full(30, 0.001))
    out = m.compute_metrics(nav, _trading_dates(nav.size))
    assert np.isnan(out["excess_return"])
    assert np.isnan(out["beta"])
    assert np.isnan(out["annual_turnover"])
    assert np.isnan(out["gross_return"])


def test_compute_metrics_empty_is_safe():
    out = m.compute_metrics(np.array([]), [])
    assert np.isnan(out["total_return"])
    assert out["monthly_returns"] == {}
