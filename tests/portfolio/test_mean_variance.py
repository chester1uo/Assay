"""Mean-variance weights: the OSQP path against SLSQP and the program's constraints."""

import dataclasses

import numpy as np
import pytest

from assay.portfolio import weights as W
from assay.portfolio.config import PortfolioBacktestConfig

pytest.importorskip("osqp")


def _cfg(**kw):
    return PortfolioBacktestConfig.preset("US", period_start="2020-01-01", period_end="2021-01-01",
                                          universe="SP500", weight_method="mv", cov_window=60, **kw)


def _case(n=100, w_obs=80, seed=0):
    rng = np.random.default_rng(seed)
    market = rng.normal(0, 0.01, (w_obs, 1))
    rets = market * rng.uniform(0.5, 1.5, n) + rng.normal(0, 0.015, (w_obs, n))
    alpha = rng.normal(size=n) * 0.01
    alpha[[3, 17]] = np.nan
    rets[:, 5] = np.nan                                  # no return history: weight 0
    cur = np.zeros(n)
    cur[rng.choice(n, 25, replace=False)] = 1 / 25
    return alpha, rets, cur


def _program(alpha, rets, cfg):
    cov = W._covariance(rets, cfg)
    idx = np.where(np.isfinite(alpha) & np.isfinite(np.diag(cov)))[0]
    return idx, cov[np.ix_(idx, idx)] + np.eye(len(idx)) * 1e-8


def _obj(alpha, cov, lam, w):
    return -(alpha @ w) + 0.5 * lam * float(w @ cov @ w)


@pytest.mark.parametrize("method", ["ledoit_wolf", "sample"])
@pytest.mark.parametrize("w_obs", [80, 1, 0])
def test_covariance_factor_is_the_covariance(method, w_obs):
    _, rets, _ = _case()
    rets = rets[:w_obs]
    cfg = _cfg(cov_method=method)
    f, d = W._covariance_factor(rets, cfg)
    cov = W._covariance(rets, cfg)
    live = np.isfinite(f).all(axis=0)
    np.testing.assert_array_equal(live, np.isfinite(np.diag(cov)))
    fl = f[:, live]
    np.testing.assert_allclose(d * np.eye(fl.shape[1]) + fl.T @ fl, cov[np.ix_(live, live)], rtol=1e-12, atol=1e-18)


@pytest.mark.parametrize("n", [100, 40])                   # window shorter / longer than the names
@pytest.mark.parametrize("long_short", [False, True])
def test_osqp_matches_slsqp(n, long_short, monkeypatch):
    alpha, rets, cur = _case(n=n)
    # no turnover cap: SLSQP's subgradient L1 constraint often fails when it binds
    cfg = _cfg(long_short=long_short, net_exposure=0.0 if long_short else 1.0, max_single_weight=0.1,
               max_turnover_per_period=2.0)
    got = W.mean_variance(alpha, rets, cur, cfg)
    monkeypatch.setattr(W, "osqp", None)
    want = W.mean_variance(alpha, rets, cur, cfg)
    idx, cov = _program(alpha, rets, cfg)
    assert not np.allclose(want, W.equal_weight(alpha, long_short, cfg.gross_exposure, cfg.net_exposure))
    assert set(np.flatnonzero(got)) <= set(idx)
    lam = cfg.mv_risk_aversion
    f_got, f_want = _obj(alpha[idx], cov, lam, got[idx]), _obj(alpha[idx], cov, lam, want[idx])
    assert f_got <= f_want + 1e-5 * abs(f_want)
    if not long_short:  # a long/short book is nearly an LP here: the optimum is flat along a few names
        np.testing.assert_allclose(got, want, atol=1e-5)
    assert abs(got.sum() - cfg.net_exposure) < 1e-4


def _capped_solver(solver, monkeypatch):
    """Solve the turnover-capped stage with Clarabel, or with OSQP alone."""
    if solver == "clarabel":
        pytest.importorskip("clarabel")
    else:
        monkeypatch.setattr(W, "clarabel", None)


@pytest.mark.parametrize("solver", ["clarabel", "osqp"])
@pytest.mark.parametrize("cap", [0.3, 0.15])
def test_binding_turnover_cap_is_met(cap, solver, monkeypatch):
    _capped_solver(solver, monkeypatch)
    alpha, rets, cur = _case()
    free = W.mean_variance(alpha, rets, cur, _cfg(max_single_weight=0.1))
    assert np.abs(free - cur).sum() > 2 * cap              # the cap binds
    cfg = _cfg(max_single_weight=0.1, max_turnover_per_period=cap)
    w = W.mean_variance(alpha, rets, cur, cfg)
    idx, cov = _program(alpha, rets, cfg)
    assert np.abs(w - cur).sum() <= 2 * cap + W._OSQP_CAPPED_TOL
    assert abs(w.sum() - 1.0) <= W._OSQP_CAPPED_TOL and w.min() >= 0.0 and w.max() <= 0.1
    # the capped optimum is no worse than the free optimum shrunk onto the cap
    shrunk = cur + (free - cur) * (2 * cap / np.abs(free - cur).sum())
    lam = cfg.mv_risk_aversion
    assert _obj(alpha[idx], cov, lam, w[idx]) < _obj(alpha[idx], cov, lam, shrunk[idx])


@pytest.mark.parametrize("cap", [0.3, 0.15])
def test_clarabel_and_osqp_agree_on_the_capped_program(cap, monkeypatch):
    pytest.importorskip("clarabel")
    alpha, rets, cur = _case()
    cfg = _cfg(max_single_weight=0.1, max_turnover_per_period=cap)
    exact = W.mean_variance(alpha, rets, cur, cfg)
    monkeypatch.setattr(W, "clarabel", None)
    admm = W.mean_variance(alpha, rets, cur, cfg)
    idx, cov = _program(alpha, rets, cfg)
    f_exact, f_admm = (_obj(alpha[idx], cov, cfg.mv_risk_aversion, w[idx]) for w in (exact, admm))
    assert abs(f_exact - f_admm) <= 1e-3 * abs(f_exact)
    assert np.abs(exact - cur).sum() <= 2 * cap + 1e-6 and abs(exact.sum() - 1.0) <= 1e-6


@pytest.mark.parametrize("solver", ["clarabel", "osqp"])
def test_unreachable_turnover_cap_leaves_the_free_optimum(solver, monkeypatch):
    _capped_solver(solver, monkeypatch)
    alpha, rets, _ = _case()
    cur = np.zeros_like(alpha)                             # from cash, a 0.1 cap cannot reach a full book
    free = W.mean_variance(alpha, rets, cur, _cfg(max_single_weight=0.1))
    capped = W.mean_variance(alpha, rets, cur, _cfg(max_single_weight=0.1, max_turnover_per_period=0.1))
    np.testing.assert_array_equal(capped, free)


def test_infeasible_box_falls_back_to_equal_weight():
    alpha, rets, cur = _case(n=30)
    cfg = dataclasses.replace(_cfg(), max_single_weight=0.01)   # 28 names x 0.01 < 1
    want = W.equal_weight(alpha, cfg.long_short, cfg.gross_exposure, cfg.net_exposure)
    np.testing.assert_array_equal(W.mean_variance(alpha, rets, cur, cfg), want)


def test_misaligned_window_falls_back_to_equal_weight():
    alpha, rets, cur = _case()
    cfg = _cfg()
    want = W.equal_weight(alpha, cfg.long_short, cfg.gross_exposure, cfg.net_exposure)
    np.testing.assert_array_equal(W.mean_variance(alpha, rets[:, :-1], cur, cfg), want)
