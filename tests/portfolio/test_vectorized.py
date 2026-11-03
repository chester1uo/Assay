"""The vectorised portfolio steps against their name-by-name definitions."""

import numpy as np
import pytest

from assay.portfolio import weights as W
from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.costs import TransactionCostModel
from assay.portfolio.execution import _LIMIT_DOWN_TOL, _LIMIT_UP_TOL, _TRADE_EPS, ExecutionSimulator
from assay.portfolio.report import Trade
from assay.portfolio.signal import process_signal

UNIVERSE = {"A": "CSI300", "US": "SP500", "HK": "HSI"}


def _cfg(market, **kw):
    return PortfolioBacktestConfig.preset(market, period_start="2020-01-01", period_end="2021-01-01",
                                          universe=UNIVERSE[market], **kw)


def test_ledoit_wolf_closed_form():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(60, 25)) * rng.uniform(0.5, 2, 25)
    x -= x.mean(axis=0)
    w, k = x.shape
    s = x.T @ x / w
    mu = np.trace(s) / k
    d2 = np.sum((s - mu * np.eye(k)) ** 2)
    b2 = min(sum(np.sum((np.outer(r, r) - s) ** 2) for r in x) / w**2, d2)
    want = b2 / d2 * mu * np.eye(k) + (1 - b2 / d2) * s
    np.testing.assert_allclose(W._ledoit_wolf_dense(x), want, rtol=1e-12, atol=1e-15)


@pytest.mark.parametrize("market", ["A", "US", "HK"])
@pytest.mark.parametrize("model", ["sqrt", "linear", "zero"])
@pytest.mark.parametrize("capital", [None, 1e5])
def test_trade_costs_match_trade_cost(market, model, capital):
    m = TransactionCostModel(_cfg(market, slippage_model=model), nav_capital=capital)
    rng = np.random.default_rng(1)
    nf = rng.uniform(0, 0.01, 200)
    nf[:3] = [0.0, np.nan, 1e-9]
    adv = rng.uniform(-0.1, 0.5, 200)
    adv[5:10] = np.nan
    sell = rng.random(200) < 0.5
    got = m.trade_costs(sell, nf, adv)
    want = [m.trade_cost("sell" if s else "buy", f, None if np.isnan(a) else a) for s, f, a in zip(sell, nf, adv)]
    assert got.tolist() == want


def _reference_execute(sim, tgt, cur, price, prev_close, lim_up_in, lim_dn_in, advf, mask, acquired, step, symbols):
    """The name-by-name walk the vectorised :meth:`ExecutionSimulator.execute` replaced."""
    cfg = sim.config
    cur = np.asarray(cur, float).copy()
    tgt = np.where(np.isfinite(tgt), tgt, cur)
    delta_all = tgt - cur
    n = len(cur)
    executed, residual, trades, total = cur.copy(), cur.copy(), [], 0.0
    diag = dict.fromkeys(("limit_hit_count", "forced_hold_count", "blocked_suspended", "blocked_adv", "n_trades"), 0.0)
    t1_on = bool(cfg.t_plus_1) and cfg.market == "A"
    explicit = lim_up_in is not None or lim_dn_in is not None
    limits_on = bool(cfg.enforce_limit_price) and (explicit or (cfg.price_limit_pct is not None and prev_close is not None))
    if limits_on:
        if explicit:
            lim_up = lim_up_in if lim_up_in is not None else np.full(n, np.inf)
            lim_dn = lim_dn_in if lim_dn_in is not None else np.full(n, -np.inf)
        else:
            lim_up, lim_dn = prev_close * (1 + cfg.price_limit_pct), prev_close * (1 - cfg.price_limit_pct)
    block = sim._blocked_trade
    for i in range(n):
        d = delta_all[i]
        if not np.isfinite(d) or abs(d) < _TRADE_EPS:
            continue
        px, sym = price[i], symbols[i]
        if not np.isfinite(px) or (mask is not None and not bool(mask[i])):
            diag["blocked_suspended"] += 1
            trades.append(block("d", sym, cur[i], tgt[i], px, "suspended"))
            residual[i] = tgt[i]
            continue
        side = "buy" if d > 0 else "sell"
        if limits_on and side == "buy" and np.isfinite(lim_up[i]) and px >= lim_up[i] * _LIMIT_UP_TOL:
            diag["limit_hit_count"] += 1
            trades.append(block("d", sym, cur[i], tgt[i], px, "limit_up"))
            residual[i] = tgt[i]
            continue
        if limits_on and side == "sell" and np.isfinite(lim_dn[i]) and px <= lim_dn[i] * _LIMIT_DOWN_TOL:
            diag["limit_hit_count"] += 1
            trades.append(block("d", sym, cur[i], tgt[i], px, "limit_down"))
            residual[i] = tgt[i]
            continue
        if t1_on and side == "sell" and acquired.get(i) == step:
            diag["forced_hold_count"] += 1
            trades.append(block("d", sym, cur[i], tgt[i], px, "t_plus_1"))
            residual[i] = tgt[i]
            continue
        fill, capped = d, False
        if advf is not None and cfg.partial_fill_handling != "force" and np.isfinite(advf[i]) and advf[i] > 0:
            frac = cfg.max_adv_fraction / advf[i]
            if frac < 1.0:
                fill, capped = d * frac, True
        if abs(fill) < _TRADE_EPS:
            diag["blocked_adv"] += 1
            trades.append(block("d", sym, cur[i], tgt[i], px, "adv_cap"))
            if cfg.partial_fill_handling == "defer":
                residual[i] = tgt[i]
            continue
        af = advf[i] * (abs(fill) / abs(d)) if advf is not None and np.isfinite(advf[i]) else None
        new_w = cur[i] + fill
        cost = sim.cost_model.trade_cost(side, abs(fill), af) * abs(fill)
        total += cost
        executed[i] = new_w
        diag["n_trades"] += 1
        if t1_on and fill > 0:
            acquired[i] = step
        residual[i] = tgt[i] if capped and cfg.partial_fill_handling == "defer" else new_w
        trades.append(Trade(date="d", symbol=sym, side=side, target_w=float(tgt[i]), exec_w=float(new_w),
                            price=float(px), qty_frac=float(fill), cost=float(cost), blocked_reason=None))
    return executed, total, residual, diag, trades


@pytest.mark.parametrize("case", range(24))
def test_execute_matches_the_name_by_name_walk(case):
    market = ("A", "US", "HK")[case % 3]
    handling = ("defer", "cancel", "force")[(case // 3) % 3]
    cfg = _cfg(market, partial_fill_handling=handling, slippage_model=("sqrt", "linear")[case % 2])
    sim = ExecutionSimulator(cfg, TransactionCostModel(cfg, nav_capital=(None, 1e6)[case % 2]))
    rng = np.random.default_rng(case)
    n = 120
    cur = np.where(rng.random(n) < 0.5, rng.random(n) / n, 0.0)
    tgt = np.where(rng.random(n) < 0.6, rng.random(n) / n, 0.0)
    tgt[rng.random(n) < 0.05] = np.nan
    tgt[:4] = cur[:4]
    px = rng.uniform(5, 50, n)
    px[rng.random(n) < 0.05] = np.nan
    prev = px / rng.uniform(0.88, 1.12, n)
    lu, ld = (prev * 1.1, prev * 0.9) if case % 4 == 0 else (None, None)
    advf = rng.uniform(0, 0.4, n) if case % 2 else None
    if advf is not None:
        advf[rng.random(n) < 0.05] = np.nan
    mask = rng.random(n) > 0.05 if case % 3 else None
    acq = {int(i): int(i % 2) for i in rng.choice(n, 30, replace=False)}
    syms = [f"S{i}" for i in range(n)]
    acq_ref = dict(acq)
    want = _reference_execute(sim, tgt, cur, px, prev, lu, ld, advf, mask, acq_ref, 1, syms)
    got = sim.execute(tgt, cur, exec_price=px, prev_close=prev, limit_up=lu, limit_down=ld, adv_fraction=advf,
                      tradable_mask=mask, acquired_step=acq, step=1, symbols=syms, date="d")
    np.testing.assert_array_equal(got.executed_weights, want[0])
    assert got.total_cost == want[1]
    np.testing.assert_array_equal(got.residual_targets, want[2])
    assert got.diag == want[3] and _rows(got.trades) == _rows(want[4]) and acq == acq_ref


def _rows(trades):
    """Trades as tuples with NaN made comparable (a suspended name has a NaN price)."""
    return [tuple("nan" if isinstance(v, float) and v != v else v for v in vars(t).values()) for t in trades]


def test_signal_on_some_dates_equals_the_full_signal():
    rng = np.random.default_rng(3)
    f = rng.normal(size=(50, 40))
    f[rng.random(f.shape) < 0.1] = np.nan
    rows = [3, 10, 11, 40]
    groups = np.arange(40) % 4
    for transform in ("rank", "zscore", "raw"):
        cfg = _cfg("US", signal_transform=transform)
        np.testing.assert_array_equal(process_signal(f[rows], cfg, groups), process_signal(f, cfg, groups)[rows])


def test_qfinverse_universes_are_valid():
    for u in ("TOP500", "TOP1000", "TOP3000"):
        assert PortfolioBacktestConfig.preset("US", period_start="2020-01-01", period_end="2021-01-01", universe=u).universe == u


def test_without_a_trade_log_only_the_records_go():
    cfg = _cfg("A")
    sim = ExecutionSimulator(cfg)
    rng = np.random.default_rng(9)
    cur, tgt, px = rng.random(50) / 50, rng.random(50) / 50, rng.uniform(5, 50, 50)
    a = sim.execute(tgt, cur, exec_price=px, prev_close=px, acquired_step={1: 0}, step=0)
    b = sim.execute(tgt, cur, exec_price=px, prev_close=px, acquired_step={1: 0}, step=0, record_trades=False)
    assert a.trades and not b.trades and a.total_cost == b.total_cost and a.diag == b.diag
    np.testing.assert_array_equal(a.executed_weights, b.executed_weights)
    np.testing.assert_array_equal(a.residual_targets, b.residual_targets)
